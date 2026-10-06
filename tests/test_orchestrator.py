import asyncio
import time

import pytest

from app.domain import Article
from app.helpdesk import DIAGNOSIS_SUBJECT, MemoryHelpdesk
from app.knowledge import FaqKnowledgeBase
from app.llm import RuleBasedLLM
from app.orchestrator import TriageService, relevant_text
from tests.helpers import FakeLogs, FakeVision, ScriptedLLM, image, make_settings, ticket, visual_and_critical

FAQS = [{"question": "¿Cómo restablecer mi contraseña de acceso?", "answer": "Usa «Olvidé mi contraseña»."}]


def build(tmp_path, tickets, *, llm=None, vision=None, logs=None, images=None, **settings):
    helpdesk = MemoryHelpdesk(tickets, images)
    service = TriageService(helpdesk, llm or RuleBasedLLM(), FaqKnowledgeBase(FAQS), vision, logs,
                            make_settings(tmp_path, **settings))
    return service, helpdesk


async def test_inquiry_is_answered_from_faq_without_specialists(tmp_path):
    vision, logs = FakeVision(), FakeLogs()
    service, helpdesk = build(tmp_path, [ticket(body="Necesito restablecer mi contraseña de acceso")], vision=vision, logs=logs)

    outcome = await service.process(1)

    assert outcome.status == "diagnosed" and outcome.type_id == 19
    assert not vision.calls and not logs.calls
    posted = helpdesk.posted[0]
    assert "Olvidé mi contraseña" in posted.body
    assert posted.body.startswith("[Diagnóstico automático]")  # sin insumos de especialistas
    assert "Tipo de ticket: Consulta" in posted.body
    assert posted.type_id == 19 and posted.subject == DIAGNOSIS_SUBJECT


async def test_incident_consults_logs_and_appends_evidence_verbatim(tmp_path):
    logs, vision = FakeLogs(), FakeVision()
    service, helpdesk = build(tmp_path, [ticket(body="El portal muestra pantalla en blanco.\nEntidad: Acme")],
                              vision=vision, logs=logs)

    outcome = await service.process(1)

    assert outcome.inputs == ["análisis de logs"]
    assert logs.calls[0].entity == "Acme" and not vision.calls
    body = helpdesk.posted[0].body
    assert "insumos: análisis de logs" in body
    assert "- LINEA CRUDA 1\n- LINEA CRUDA 2" in body  # evidencia literal, después del texto del modelo
    assert "Tipo de ticket: Incidente" in body


async def test_visual_ticket_uses_vision_specialist_with_attachments(tmp_path):
    vision = FakeVision()
    t = ticket(body="Adjunto la imagen con el boceto del banner", subject="Diseño")
    service, helpdesk = build(tmp_path, [t], vision=vision, images={1: [image()]})

    await service.process(1)

    assert len(vision.calls[0].images) == 1
    body = helpdesk.posted[0].body
    assert "insumos: análisis visual" in body and "Hallazgos del análisis visual" in body and "Contraste" in body


async def test_security_alert_is_flagged_critical_and_consults_logs(tmp_path):
    logs = FakeLogs()
    service, helpdesk = build(tmp_path, [ticket(body="Posible ransomware: archivos cifrados")], logs=logs)
    await service.process(1)
    assert logs.calls
    assert "ALERTA CRÍTICA" in helpdesk.posted[0].body


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"state": "open"}, "ticket_not_new"),
        ({"articles": []}, "no_articles"),
        ({"articles": [Article(sender="customer", body="a"), Article(sender="agent", body="b"),
                       Article(sender="customer", body="c")]}, "too_many_articles"),
    ],
)
async def test_guards_skip_tickets_that_should_not_be_touched(tmp_path, kwargs, reason):
    service, helpdesk = build(tmp_path, [ticket(**kwargs)])
    outcome = await service.process(1)
    assert (outcome.status, outcome.reason) == ("skipped", reason)
    assert not helpdesk.posted


async def test_unknown_ticket_is_skipped(tmp_path):
    service, _ = build(tmp_path, [])
    assert (await service.process(99)).reason == "ticket_not_found"


async def test_webhook_redelivery_does_not_diagnose_twice(tmp_path):
    """El helpdesk puede reenviar el evento: la nota propia es la marca de idempotencia."""
    service, helpdesk = build(tmp_path, [ticket(body="Consulta general sin más")])
    assert (await service.process(1)).status == "diagnosed"
    second = await service.process(1)
    assert (second.status, second.reason) == ("skipped", "already_diagnosed")
    assert len(helpdesk.posted) == 1


async def test_concurrent_duplicate_events_process_the_ticket_once(tmp_path):
    vision = FakeVision(delay=0.1)
    service, helpdesk = build(tmp_path, [ticket(body="Adjunto la imagen con el boceto del banner")], vision=vision)
    first, second = await asyncio.gather(service.process(1), service.process(1))
    assert sorted(o.status for o in (first, second)) == ["diagnosed", "skipped"]
    assert len(helpdesk.posted) == 1


async def test_specialists_run_in_parallel_not_in_sequence(tmp_path):
    vision, logs = FakeVision(delay=0.3), FakeLogs(delay=0.3)
    service, helpdesk = build(tmp_path, [ticket(body="cualquier texto")], llm=ScriptedLLM(visual_and_critical()),
                              vision=vision, logs=logs, images={1: [image()]})
    started = time.monotonic()
    outcome = await service.process(1)
    elapsed = time.monotonic() - started

    assert outcome.inputs == ["análisis visual", "análisis de logs"]
    assert elapsed < 0.55, f"{elapsed:.2f}s: se ejecutaron en secuencia"
    assert len(helpdesk.posted) == 1


async def test_slow_specialist_degrades_instead_of_blocking_the_diagnosis(tmp_path):
    logs = FakeLogs(delay=1.0)
    service, helpdesk = build(tmp_path, [ticket(body="El sitio muestra un error fatal.\nEntidad: Acme")], logs=logs,
                              logs_timeout_seconds=0.05)
    outcome = await service.process(1)
    assert outcome.status == "diagnosed" and outcome.inputs == []
    assert "omitido por latencia" in helpdesk.posted[0].body


async def test_failing_specialist_does_not_prevent_the_diagnosis(tmp_path):
    service, helpdesk = build(tmp_path, [ticket(body="El sitio muestra un error fatal.\nEntidad: Acme")],
                              logs=FakeLogs(fail=True))
    assert (await service.process(1)).status == "diagnosed"
    assert helpdesk.posted


async def test_type_kill_switch_computes_type_but_does_not_send_it(tmp_path):
    service, helpdesk = build(tmp_path, [ticket(body="El sitio muestra un error fatal")], ticket_type_enabled=False)
    await service.process(1)
    assert helpdesk.posted[0].type_id is None and helpdesk.types == {}
    assert "Tipo de ticket: Incidente" in helpdesk.posted[0].body  # el equipo lo asigna a mano


async def test_type_invented_by_the_model_is_never_sent(tmp_path):
    service, helpdesk = build(tmp_path, [ticket(body="Consulta general sin más")], llm=ScriptedLLM(report_type=99))
    await service.process(1)
    assert helpdesk.posted[0].type_id == 19  # se conserva el de la clasificación


async def test_report_type_overrides_classification_when_valid(tmp_path):
    service, helpdesk = build(tmp_path, [ticket(body="Consulta general sin más")], llm=ScriptedLLM(report_type=10))
    await service.process(1)
    assert helpdesk.posted[0].type_id == 10


async def test_llm_failure_posts_nothing_and_reports_error(tmp_path):
    service, helpdesk = build(tmp_path, [ticket()], llm=ScriptedLLM(boom=True))
    outcome = await service.process(1)
    assert outcome.status == "error" and not helpdesk.posted
    # y el ticket no queda "atascado" como en proceso
    assert (await service.process(1)).status == "error"


def test_relevant_text_prefers_last_human_message_and_respects_budget():
    articles = [Article(sender="customer", subject="A", body="primero"), Article(sender="system", subject="S", body="auto")]
    assert "primero" in relevant_text(articles, 1000) and "auto" not in relevant_text(articles, 1000)
    assert len(relevant_text([Article(sender="customer", body="x" * 5000)], 100)) == 100
