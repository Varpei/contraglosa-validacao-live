#!/usr/bin/env python3
"""Servidor local, sem dependências externas, para validação de clínicas.

Execute com: python3 app_codespaces.py

O Codespaces pode encaminhar a porta 8080 publicamente. O identificador
persistente é criado no navegador e enviado somente como hash SHA-256; o
servidor usa esse hash para impedir uma segunda resposta no mesmo dispositivo.
"""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import unicodedata
from urllib.parse import parse_qs, urlsplit
from threading import Lock


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "dados"
RESPONSES_FILE = DATA_DIR / "respostas_clinicas.jsonl"
SLOTS_FILE = DATA_DIR / "slots_clinicas.json"
SCRIPT_FILE = ROOT / "docs" / "ROTEIRO-ENTREVISTA.md"
INDIVIDUALS_DIR = ROOT / "docs" / "pesquisas"
LEADER_TOKEN_FILE = DATA_DIR / ".leader_token"
HOST = "0.0.0.0"
PORTA = int(os.environ.get("PORT", 8080))
MAX_BODY_BYTES = 16 * 1024
# O Codespaces/DNS precisa apontar estes endereços para a porta 8080.
PUBLIC_FORM_URL = "https://ackcit-hackathon-contra-glosas-pesquisa-oficial.github.dev"
ADMIN_URL = "https://admin-ackcit-hackathon-contra-glosas-pesquisa-oficial.github.dev"
# Altere somente este valor para definir a senha do painel executivo.
LEADER_PASSWORD = "AKCIT*CEITA@2026@@*&"
HEALTH_PLANS = [
    "Amil",
    "Bradesco Saúde",
    "SulAmérica",
    "Unimed",
    "NotreDame Intermédica",
    "Hapvida",
    "Plano regional ou cooperativa",
    "Atendimento particular",
    "Outro plano",
]

# As nove perguntas são a fonte única de verdade usada pelo HTML e pela API.
QUESTIONS = [
    {
        "id": "perfil",
        "title": "Qual é o seu perfil nesta validação?",
        "options": ["Clínica médica", "Médico(a) autônomo(a)", "Membro da equipe"],
    },
    {
        "id": "volume",
        "title": "Quantas glosas sua operação analisa em um mês típico?",
        "options": ["Até 20", "21 a 100", "101 a 500", "Mais de 500", "Não sei informar"],
    },
    {
        "id": "tempo",
        "title": "Quanto tempo a equipe dedica à análise de cada glosa?",
        "options": ["Até 5 minutos", "6 a 15 minutos", "16 a 30 minutos", "Mais de 30 minutos"],
    },
    {
        "id": "abandono",
        "title": "No último trimestre, houve glosas abandonadas por falta de tempo ou informação?",
        "options": ["Nenhuma", "Poucas (1% a 25%)", "Algumas (26% a 50%)", "Muitas (mais de 50%)"],
    },
    {
        "id": "retrabalho",
        "title": "Com que frequência a equipe refaz uma análise por falta de contexto?",
        "options": ["Nunca", "Raramente", "Às vezes", "Frequentemente"],
    },
    {
        "id": "visibilidade",
        "title": "Como você acompanha o motivo das glosas?",
        "options": ["Planilha", "Sistema próprio", "Sistema terceirizado", "Não há acompanhamento"],
    },
    {
        "id": "prioridade",
        "title": "Qual seria o maior ganho de uma ferramenta de validação?",
        "options": ["Reduzir tempo", "Recuperar receita", "Padronizar análise", "Treinar a equipe"],
    },
    {
        "id": "interesse",
        "title": "Você participaria de um teste online da solução?",
        "options": ["Sim, quero testar", "Talvez, quero saber mais", "Não neste momento"],
    },
    {
        "id": "dor",
        "title": "Qual frase descreve melhor a dor da sua operação?",
        "options": [
            "Perdemos receita porque não conseguimos analisar todas as glosas.",
            "A equipe gasta tempo demais procurando documentos e regras.",
            "Não temos clareza sobre o motivo real das glosas.",
            "Hoje não temos uma dor relevante com glosas.",
        ],
    },
]
QUESTION_IDS = {question["id"] for question in QUESTIONS}
OPTIONS_BY_ID = {
    question["id"]: set(question["options"]) for question in QUESTIONS
}
INTEREST_OPTIONS = {"Sim, quero testar", "Talvez, quero saber mais"}
ABANDONED_OPTIONS = {"Poucas (1% a 25%)", "Algumas (26% a 50%)", "Muitas (mais de 50%)"}
DEFAULT_SLOTS = {str(number): {"status": "disponivel"} for number in range(1, 11)}

STATE_LOCK = Lock()
LEADER_SESSIONS = set()


def utc_now():
    """Retorna um timestamp consistente e legível em UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SLOTS_FILE.exists():
        atomic_write_json(SLOTS_FILE, DEFAULT_SLOTS)
    if not RESPONSES_FILE.exists():
        RESPONSES_FILE.touch()
    if not LEADER_TOKEN_FILE.exists():
        atomic_write_text(LEADER_TOKEN_FILE, secrets.token_urlsafe(32))


def leader_token():
    return LEADER_TOKEN_FILE.read_text(encoding="utf-8").strip()


def is_leader_request(path):
    token = parse_qs(urlsplit(path).query).get("leader", [""])[0]
    return bool(token) and hmac.compare_digest(token, leader_token())


def is_admin_route(path):
    return urlsplit(path).path == "/admin"


def has_leader_route(path):
    return is_admin_route(path) or is_leader_request(path)


def is_authenticated_leader(handler):
    cookie = handler.headers.get("Cookie", "")
    session_id = next(
        (
            part.split("=", 1)[1]
            for part in cookie.split("; ")
            if part.startswith("leader_session=") and "=" in part
        ),
        "",
    )
    with STATE_LOCK:
        return session_id in LEADER_SESSIONS


def request_cookie(handler, name):
    for item in handler.headers.get("Cookie", "").split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value
    return ""


def device_hash(handler):
    device_id = request_cookie(handler, "validation_device")
    if not device_id:
        return ""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


def atomic_write_text(path, content):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_jsonl():
    if not RESPONSES_FILE.exists():
        return []
    responses = []
    for line in RESPONSES_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            responses.append(value)
    return responses


def read_slots():
    try:
        slots = json.loads(SLOTS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        slots = {}
    result = {}
    for number in range(1, 11):
        slot = slots.get(str(number), {}) if isinstance(slots, dict) else {}
        result[str(number)] = {
            "status": "eliminada" if slot.get("status") == "eliminada" else "disponivel",
            "updated_at": slot.get("updated_at"),
        }
    return result


def markdown_escape(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def slugify(value):
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug or "respondente"


def individual_markdown(index, response):
    answers = response.get("answers", {})
    lines = [
        f"# Entrevista #{index:02d}",
        "",
        f"- **Data:** {markdown_escape(response.get('submitted_at', '—'))}",
        f"- **Nome:** {markdown_escape(response.get('name', '—'))}",
        f"- **CRM:** {markdown_escape(response.get('crm', '—'))}",
        f"- **Plano de saúde:** {markdown_escape(response.get('health_plan', '—'))}",
        "",
        "## Respostas",
        "",
    ]
    for number, question in enumerate(QUESTIONS, 1):
        lines.extend(
            [
                f"### {number}. {question['title']}",
                "",
                f"> {markdown_escape(answers.get(question['id'], '—'))}",
                "",
            ]
        )
    return "\n".join(lines)


def generate_markdown_exports(responses, slots):
    total = len(responses)
    abandoned = sum(
        1 for response in responses
        if response.get("answers", {}).get("abandono") in ABANDONED_OPTIONS
    )
    interested = sum(
        1 for response in responses
        if response.get("answers", {}).get("interesse") in INTEREST_OPTIONS
    )
    abandoned_percentage = (abandoned / total * 100) if total else 0
    interest_percentage = (interested / total * 100) if total else 0
    generated_at = utc_now()
    eliminated = sum(1 for slot in slots.values() if slot["status"] == "eliminada")
    personas = {}
    for response in responses:
        persona = response.get("answers", {}).get("perfil", "Não informado")
        personas[persona] = personas.get(persona, 0) + 1

    lines = [
        "# Roteiro de Entrevista — Validação de Mercado",
        "",
        "> Documento atualizado automaticamente por `app_codespaces.py`.",
        "",
        "## Indicadores para o pitch",
        "",
        f"- **Respostas válidas:** {total}",
        f"- **Glosas abandonadas:** {abandoned}/{total} ({abandoned_percentage:.2f}%)",
        f"- **Sinal de interesse:** {interested}/{total} ({interest_percentage:.2f}%)",
        f"- **Slots eliminados:** {eliminated}/10",
        f"- **Atualizado em:** {generated_at}",
        "",
        "## Distribuição por persona",
        "",
    ]
    if personas:
        lines.extend(f"- **{markdown_escape(persona)}:** {count} ({count / total * 100:.2f}%)" for persona, count in sorted(personas.items()))
    else:
        lines.append("- Nenhuma resposta registrada.")
    lines.extend(
        [
            "",
        "### Critério dos indicadores",
        "",
        "Glosa abandonada = resposta diferente de `Nenhuma` na pergunta sobre o último trimestre.",
        "Sinal de interesse = `Sim, quero testar` ou `Talvez, quero saber mais`.",
        "As porcentagens usam todas as respostas válidas e são recalculadas a cada envio.",
        "",
        "## Roteiro click-only",
        "",
        ]
    )
    for number, question in enumerate(QUESTIONS, 1):
        lines.append(f"{number}. **{question['title']}**")
        lines.extend(f"   - {option}" for option in question["options"])
    lines.extend(
        [
            "",
            "## Fila de Clínicas / Slots",
            "",
            "| Slot | Status | Atualizado em |",
            "|---:|---|---|",
        ]
    )
    for number in range(1, 11):
        slot = slots[str(number)]
        status = "Eliminada" if slot["status"] == "eliminada" else "Disponível"
        lines.append(f"| #{number} | {status} | {slot.get('updated_at') or '—'} |")
    lines.extend(["", "## Resumo das entrevistas", ""])
    if not responses:
        lines.append("Nenhuma entrevista registrada.")
    for index, response in enumerate(responses, 1):
        answers = response.get("answers", {})
        lines.extend(
            [
                f"### Entrevista #{index:02d} — {markdown_escape(response.get('name', '—'))}",
                "",
                f"- **Data:** {markdown_escape(response.get('submitted_at', '—'))}",
                f"- **CRM:** {markdown_escape(response.get('crm', '—'))}",
                f"- **Plano de saúde:** {markdown_escape(response.get('health_plan', '—'))}",
                f"- **Persona:** {markdown_escape(answers.get('perfil', '—'))}",
                f"- **Tempo gasto:** {markdown_escape(answers.get('tempo', '—'))}",
                f"- **Frase de dor:** {markdown_escape(answers.get('dor', '—'))}",
                "",
            ]
        )
    atomic_write_text(SCRIPT_FILE, "\n".join(lines))
    INDIVIDUALS_DIR.mkdir(parents=True, exist_ok=True)
    for old_file in INDIVIDUALS_DIR.glob("entrevista_*.md"):
        old_file.unlink()
    individual_files = {}
    for index, response in enumerate(responses, 1):
        relative_path = f"docs/pesquisas/entrevista_{index:02d}.md"
        content = individual_markdown(index, response)
        atomic_write_text(ROOT / relative_path, content)
        individual_files[relative_path] = content
    return "\n".join(lines), individual_files


def rebuild_script(responses, slots):
    """Compatibilidade com chamadas existentes; também gera os exports."""
    return generate_markdown_exports(responses, slots)


def dashboard_payload():
    with STATE_LOCK:
        responses = read_jsonl()
        slots = read_slots()
        generate_markdown_exports(responses, slots)
    total = len(responses)
    abandoned = sum(
        response.get("answers", {}).get("abandono") in ABANDONED_OPTIONS
        for response in responses
    )
    interested = sum(
        response.get("answers", {}).get("interesse") in INTEREST_OPTIONS
        for response in responses
    )
    return {
        "total": total,
        "abandoned": abandoned,
        "interested": interested,
        "abandoned_percentage": round(abandoned / total * 100, 2) if total else 0,
        "interest_percentage": round(interested / total * 100, 2) if total else 0,
        "slots": slots,
        "responses": [
            {
                "number": number,
                "submitted_at": response.get("submitted_at", ""),
                "name": response.get("name", "—"),
                "crm": response.get("crm", "—"),
                "health_plan": response.get("health_plan", "—"),
                "persona": response.get("answers", {}).get("perfil", "—"),
                "time_spent": response.get("answers", {}).get("tempo", "—"),
                "pain": response.get("answers", {}).get("dor", "—"),
                "answers": response.get("answers", {}),
            }
            for number, response in enumerate(responses, 1)
        ],
        "updated_at": utc_now(),
    }


def export_payload():
    with STATE_LOCK:
        responses = read_jsonl()
        slots = read_slots()
        consolidated, individual_files = generate_markdown_exports(responses, slots)
    return {
        "consolidado_md": consolidated,
        "arquivos_individuais": individual_files,
        "respostas_raw": responses,
    }


def page_html(is_leader=False, authenticated=False):
    questions_html = []
    for number, question in enumerate(QUESTIONS, 1):
        options = []
        for option in question["options"]:
            encoded = json.dumps(option, ensure_ascii=False)
            options.append(
                f'<label class="option"><input type="radio" name="{question["id"]}" '
                f'value={encoded} required><span>{option}</span></label>'
            )
        questions_html.append(
            f'<fieldset><legend>{number}. {question["title"]}</legend>'
            + "".join(options)
            + "</fieldset>"
        )

    return """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Validação de Mercado — Clínicas</title>
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; background: #f4f7fb; color: #172033; --identity-control-width: 100%; --identity-control-height: 20px; }
body { margin: 0; } main { max-width: 980px; margin: auto; padding: 24px 16px 60px; }
header, section, fieldset { background: #fff; border: 1px solid #dce4ef; border-radius: 14px; padding: 20px; margin: 0 0 16px; }
h1 { margin-top: 0; } h2 { margin-top: 0; font-size: 1.2rem; }
.muted { color: #5b687a; } .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.metric { background: #eef5ff; border-radius: 10px; padding: 12px; } .metric strong { display: block; font-size: 1.5rem; }
.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.tab { background: #e2e8f0; color: #172033; } .tab.active { background: #1463d5; color: #fff; }
.tab-panel.hidden { display: none; }
fieldset { display: grid; gap: 10px; } legend { font-weight: 700; padding: 0 5px; }
.identity-field { display: grid; gap: 6px; min-width: 0; font-weight: 600; }
.identity-field input, .identity-field select { display: block; width: var(--identity-control-width); max-width: 100%; min-width: 0; height: var(--identity-control-height); box-sizing: border-box; padding: 12px; border: 1px solid #b9c5d5; border-radius: 8px; font: inherit; background: #fff; }
.option { display: flex; gap: 10px; align-items: center; border: 1px solid #dce4ef; border-radius: 10px; padding: 13px; cursor: pointer; }
.option:has(input:checked) { border-color: #1463d5; background: #eef5ff; } input { accent-color: #1463d5; transform: scale(1.2); }
button { border: 0; border-radius: 9px; padding: 12px 16px; background: #1463d5; color: white; font-weight: 700; cursor: pointer; }
button.secondary { background: #64748b; } button:disabled { opacity: .5; cursor: not-allowed; }
.status { padding: 14px; border-radius: 9px; background: #fff5d6; margin: 12px 0; } .hidden { display: none !important; }
.slot { display: flex; justify-content: space-between; align-items: center; gap: 8px; border-bottom: 1px solid #e8edf4; padding: 10px 0; }
.slot:last-child { border-bottom: 0; } .eliminada { color: #64748b; text-decoration: line-through; }
.table-wrap { overflow-x: auto; } table { width: 100%; border-collapse: collapse; min-width: 680px; }
th, td { text-align: left; vertical-align: top; padding: 11px 8px; border-bottom: 1px solid #e8edf4; }
th { color: #5b687a; font-size: .85rem; } td.pain { min-width: 280px; }
.respondent-card { border: 1px solid #dce4ef; border-radius: 10px; padding: 14px; margin-bottom: 12px; }
.respondent-card h3 { margin: 0 0 8px; } .respondent-card dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px 16px; margin: 0; }
.respondent-card dt { color: #5b687a; font-size: .85rem; } .respondent-card dd { margin: 0; font-weight: 600; }
</style>
</head>
<body><main>
<header>
<h1>Validação de Mercado para Clínicas</h1>
<p class="muted">Obrigado por aceitar a fazer essa entrevista com a gente, vai ajudar bastante. Tenha uma boa tarde!</p>
</header>
<div id="already" class="status hidden"><strong>Esta clínica/usuário já registrou a resposta neste dispositivo. Obrigado pela participação!</strong></div>
""" + (
"""<section>
<h2>Acesso do líder</h2>
<p class="muted">Informe a senha definida na linha <code>LEADER_PASSWORD</code> do arquivo <code>app_codespaces.py</code>.</p>
<form id="leader-login">
<input name="password" type="password" autocomplete="current-password" required>
<button type="submit">Entrar no painel</button>
</form>
<div id="login-status" class="status hidden"></div>
</section>
"""
    if is_leader and not authenticated else ""
) + """
<nav class="tabs" aria-label="Navegação principal">
<button type="button" class="tab active" data-tab="form-tab">📋 Formulário de Validação (Click-Only)</button>
""" + (
    '<button type="button" class="tab" data-tab="dashboard-tab">📊 Painel Executivo do Líder (Dashboard em Tempo Real)</button>'
    if is_leader and authenticated else ""
) + """
</nav>
<div id="form-tab" class="tab-panel">
<section id="survey">
<h2>Pesquisa rápida</h2>
<p class="muted">As nove perguntas são de múltipla escolha. Para conferência, informe seus dados profissionais; eles ficam visíveis somente no painel protegido do líder.</p>
<form id="survey-form">
<fieldset>
<legend>Identificação para conferência</legend>
<label class="identity-field" for="doctor-name">Nome completo
<input id="doctor-name" name="name" type="text" maxlength="120" autocomplete="name" required>
</label>
<label class="identity-field" for="doctor-crm">CRM
<input id="doctor-crm" name="crm" type="text" maxlength="30" autocomplete="off" placeholder="Ex.: CRM-SP 123456" required>
</label>
<label class="identity-field" for="health-plan">Plano de saúde utilizado
<select id="health-plan" name="health_plan" required>
<option value="">Selecione uma opção</option>
""" + "".join(
    f'<option value="{plan}">{plan}</option>' for plan in HEALTH_PLANS
) + """
</select></label>
<label id="custom-plan-field" class="identity-field hidden" for="custom-plan">Informe o plano de saúde
<input id="custom-plan" name="custom_plan" type="text" maxlength="80" autocomplete="organization">
</label>
<label><input name="consent" type="checkbox" required> Autorizo o armazenamento desses dados para conferência desta validação.</label>
</fieldset>
""" + "".join(questions_html) + """
<button type="submit">Registrar resposta</button>
</form><div id="form-status" class="status hidden"></div>
</section>
<section>
<h2>Fila de Clínicas / Slots</h2>
<p class="muted">Use os botões para eliminar uma clínica da fila. A alteração aparece para todos que acessarem este link.</p>
<div id="slots"></div>
</section>
</div>
""" + (
"""<div id="dashboard-tab" class="tab-panel hidden">
<section>
<h2>Painel Executivo do Líder</h2>
<p class="muted">Consolidado atualizado automaticamente a cada cinco segundos. Nenhum hash ou identificador técnico é exibido.</p>
<div id="metrics" class="grid"></div>
<p><button id="open-responses" type="button">Abrir página de respostas</button>
<button id="copy-report" type="button">Copiar Relatório p/ Pitch (Markdown)</button>
<button id="download-consolidated" type="button">📥 Baixar Consolidado (.md)</button>
<button id="download-individuals" type="button">📂 Baixar Todas as Entrevistas Individuais (.md)</button></p>
<div id="copy-status" class="status hidden"></div>
</section>
<section>
<h2>Respostas consolidadas</h2>
<div id="respondents"></div>
<div class="table-wrap"><table>
<thead><tr><th>#</th><th>Data</th><th>Nome</th><th>CRM</th><th>Plano de saúde</th><th>Persona</th><th>Tempo gasto</th><th>Frase literal de dor</th></tr></thead>
<tbody id="responses"></tbody>
</table></div>
</section>
</div>
"""
    if is_leader and authenticated else ""
) + """
</main>
<script>
const questionIds = """ + json.dumps([question["id"] for question in QUESTIONS]) + """;
const form = document.getElementById("survey-form");
const statusBox = document.getElementById("form-status");
const alreadyBox = document.getElementById("already");
const surveyBox = document.getElementById("survey");
let latestDashboard = null;
const healthPlan = document.getElementById("health-plan");
const customPlanField = document.getElementById("custom-plan-field");
const customPlan = document.getElementById("custom-plan");

healthPlan.addEventListener("change", () => {
  const isCustom = healthPlan.value === "Outro plano";
  customPlanField.classList.toggle("hidden", !isCustom);
  customPlan.required = isCustom;
  if (!isCustom) customPlan.value = "";
});

const leaderLogin = document.getElementById("leader-login");
if (leaderLogin) leaderLogin.addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/leader-login" + window.location.search, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: leaderLogin.password.value})
  });
  const status = document.getElementById("login-status");
  status.textContent = response.ok ? "Acesso autorizado. Recarregando..." : "Senha incorreta.";
  status.classList.remove("hidden");
  if (response.ok) window.location.reload();
});

function showStatus(message, isError = false) {
  statusBox.textContent = message;
  statusBox.classList.remove("hidden");
  statusBox.style.background = isError ? "#ffe4e6" : "#fff5d6";
}

async function browserHash() {
  let id = localStorage.getItem("validacao_browser_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("validacao_browser_id", id);
  }
  const bytes = new TextEncoder().encode(id);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function renderMetrics(data) {
  document.getElementById("metrics").innerHTML =
    `<div class="metric"><strong>${data.total}</strong>respostas</div>` +
    `<div class="metric"><strong>${data.abandoned_percentage.toFixed(2)}%</strong>glosas abandonadas</div>` +
    `<div class="metric"><strong>${data.interest_percentage.toFixed(2)}%</strong>sinal de interesse</div>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[character]));
}

function renderResponses(responses) {
  const target = document.getElementById("responses");
  const cards = document.getElementById("respondents");
  cards.innerHTML = responses.length ? responses.map(response =>
    `<article class="respondent-card"><h3>Respondente #${response.number} — ${escapeHtml(response.name)}</h3>` +
    `<dl><div><dt>Data</dt><dd>${escapeHtml(response.submitted_at.replace("T", " "))}</dd></div>` +
    `<div><dt>CRM</dt><dd>${escapeHtml(response.crm)}</dd></div>` +
    `<div><dt>Plano de saúde</dt><dd>${escapeHtml(response.health_plan)}</dd></div>` +
    `<div><dt>Persona</dt><dd>${escapeHtml(response.persona)}</dd></div>` +
    `<div><dt>Tempo gasto</dt><dd>${escapeHtml(response.time_spent)}</dd></div>` +
    `<div><dt>Frase de dor</dt><dd>${escapeHtml(response.pain)}</dd></div></dl></article>`
  ).join("") : '<p class="muted">Nenhuma entrevista registrada ainda.</p>';
  target.innerHTML = responses.length ? responses.map(response =>
    `<tr><td>${response.number}</td><td>${escapeHtml(response.submitted_at.replace("T", " "))}</td>` +
    `<td>${escapeHtml(response.name)}</td><td>${escapeHtml(response.crm)}</td>` +
    `<td>${escapeHtml(response.health_plan)}</td>` +
    `<td>${escapeHtml(response.persona)}</td><td>${escapeHtml(response.time_spent)}</td>` +
    `<td class="pain">${escapeHtml(response.pain)}</td></tr>`
  ).join("") : '<tr><td colspan="8" class="muted">Nenhuma entrevista registrada ainda.</td></tr>';
}

function renderSlots(slots) {
  const target = document.getElementById("slots");
  target.innerHTML = Object.entries(slots).map(([number, slot]) => {
    const eliminated = slot.status === "eliminada";
    return `<div class="slot"><span class="${eliminated ? "eliminada" : ""}">Slot #${number} — ${eliminated ? "Eliminada" : "Disponível"}</span>` +
      `<button class="${eliminated ? "secondary" : ""}" data-slot="${number}" ${eliminated ? "disabled" : ""}>${eliminated ? "Concluído" : "Eliminar"}</button></div>`;
  }).join("");
  target.querySelectorAll("button[data-slot]").forEach(button => {
    button.addEventListener("click", () => updateSlot(button.dataset.slot));
  });
}

async function refreshDashboard() {
  const response = await fetch("/api/dashboard" + window.location.search, {cache: "no-store"});
  if (!response.ok) throw new Error("Não foi possível atualizar o painel.");
  const data = await response.json();
  latestDashboard = data;
  renderMetrics(data); renderSlots(data.slots);
  renderResponses(data.responses);
}

async function updateSlot(number) {
  const response = await fetch("/api/slots", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({slot: Number(number), status: "eliminada"})
  });
  if (!response.ok) { alert("Não foi possível atualizar este slot."); return; }
  refreshDashboard().catch(() => {});
}

async function checkDevice() {
  if (localStorage.getItem("validacao_respondido") === "1") {
    surveyBox.classList.add("hidden"); alreadyBox.classList.remove("hidden");
  }
}

function activateTab(tabId) {
  document.querySelectorAll(".tab").forEach(tab => tab.classList.toggle("active", tab.dataset.tab === tabId));
  document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.toggle("hidden", panel.id !== tabId));
  if (tabId === "form-tab") document.getElementById("survey").scrollIntoView({behavior: "smooth"});
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

const copyReport = document.getElementById("copy-report");
function downloadText(filename, content) {
  const blob = new Blob([content], {type: "text/markdown;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}
const downloadConsolidated = document.getElementById("download-consolidated");
if (downloadConsolidated) downloadConsolidated.addEventListener("click", async () => {
  const response = await fetch("/api/exportar-mds", {cache: "no-store"});
  if (!response.ok) { alert("Não foi possível exportar o consolidado."); return; }
  const data = await response.json();
  downloadText("ROTEIRO-ENTREVISTA.md", data.consolidado_md);
});
const downloadIndividuals = document.getElementById("download-individuals");
if (downloadIndividuals) downloadIndividuals.addEventListener("click", async () => {
  const response = await fetch("/api/exportar-mds", {cache: "no-store"});
  if (!response.ok) { alert("Não foi possível exportar as entrevistas."); return; }
  const data = await response.json();
  Object.entries(data.arquivos_individuais).forEach(([path, content]) => {
    downloadText(path.split("/").pop(), content);
  });
});
const openResponses = document.getElementById("open-responses");
if (openResponses) openResponses.addEventListener("click", () => {
  activateTab("dashboard-tab");
  document.getElementById("responses").scrollIntoView({behavior: "smooth"});
});
if (copyReport) copyReport.addEventListener("click", async () => {
  const report = [
    "# Relatório executivo — Validação de Mercado",
    "",
    `- **Total de entrevistas:** ${latestDashboard?.total ?? 0}`,
    `- **Glosas abandonadas:** ${(latestDashboard?.abandoned_percentage ?? 0).toFixed(2)}%`,
    `- **Sinal de interesse imediato:** ${(latestDashboard?.interest_percentage ?? 0).toFixed(2)}%`,
    "",
    "## Respostas",
    "",
    "| # | Data | Nome | CRM | Plano de saúde | Persona | Tempo gasto | Frase literal de dor |",
    "|---:|---|---|---|---|---|---|---|---|",
    ...(latestDashboard?.responses ?? []).map(response =>
      `| ${response.number} | ${response.submitted_at} | ${response.name} | ${response.crm} | ${response.health_plan} | ${response.persona} | ${response.time_spent} | ${response.pain.replaceAll("|", "\\|")} |`
    )
  ].join("\n");
  try {
    await navigator.clipboard.writeText(report);
    const copyStatus = document.getElementById("copy-status");
    copyStatus.textContent = "Relatório Markdown copiado para a área de transferência.";
    copyStatus.classList.remove("hidden");
  } catch (error) {
    const copyStatus = document.getElementById("copy-status");
    copyStatus.textContent = "Não foi possível copiar automaticamente neste navegador.";
    copyStatus.classList.remove("hidden");
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const answers = {};
  for (const id of questionIds) {
    const selected = form.querySelector(`input[name="${id}"]:checked`);
    if (!selected) { showStatus("Escolha uma opção em todas as perguntas.", true); return; }
    answers[id] = selected.value;
  }
  const submitButton = form.querySelector("button[type=submit]");
  submitButton.disabled = true;
  try {
    const payload = {
      browser_id_hash: await browserHash(),
      name: form.name.value.trim(),
      crm: form.crm.value.trim(),
      health_plan: form.health_plan.value,
      custom_plan: form.custom_plan.value.trim(),
      consent: form.consent.checked,
      answers
    };
    const response = await fetch("/api/respostas", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (response.status === 409) {
      localStorage.setItem("validacao_respondido", "1");
      surveyBox.classList.add("hidden"); alreadyBox.classList.remove("hidden"); return;
    }
    if (!response.ok) throw new Error(result.error || "Não foi possível registrar a resposta.");
    localStorage.setItem("validacao_respondido", "1");
    surveyBox.classList.add("hidden"); alreadyBox.classList.remove("hidden");
    await refreshDashboard();
  } catch (error) {
    showStatus(error.message, true); submitButton.disabled = false;
  }
});

checkDevice();
""" + (
"""refreshDashboard().catch(() => {});
setInterval(() => refreshDashboard().catch(() => {}), 5000);"""
    if is_leader else ""
) + """
</script>
</body></html>"""


class AppHandler(BaseHTTPRequestHandler):
    """Rotas HTTP do formulário e do painel compartilhado."""

    server_version = "ValidacaoClinicas/1.0"

    def log_message(self, format_string, *args):
        print(f"[{utc_now()}] {self.address_string()} - {format_string % args}")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_json(200, {"status": "ok"})
        elif path == "/api/exportar-mds" and is_authenticated_leader(self):
            self.send_json(200, export_payload())
        elif path == "/api/exportar-mds":
            self.send_json(403, {"error": "Exportação disponível somente para o líder autenticado."})
        elif path in ("/", "/index.html", "/entrevista", "/admin"):
            leader_access = has_leader_route(self.path)
            body = page_html(
                leader_access,
                leader_access and is_authenticated_leader(self),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
            )
            if not request_cookie(self, "validation_device"):
                self.send_header(
                    "Set-Cookie",
                    f"validation_device={secrets.token_urlsafe(32)}; Max-Age=31536000; HttpOnly; SameSite=Lax; Path=/",
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/dashboard" and is_authenticated_leader(self):
            self.send_json(200, dashboard_payload())
        elif path == "/api/dashboard":
            self.send_json(403, {"error": "Painel disponível somente com o link e a senha do líder."})
        else:
            self.send_json(404, {"error": "Rota não encontrada."})

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Tamanho de requisição inválido.")
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("Requisição fora do limite permitido.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = self.read_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Payload inválido."})
            return
        if path == "/api/leader-login":
            self.leader_login(payload)
        elif path == "/api/respostas":
            self.save_response(payload)
        elif path == "/api/slots":
            self.update_slot(payload)
        else:
            self.send_json(404, {"error": "Rota não encontrada."})

    def leader_login(self, payload):
        password = payload.get("password")
        if not isinstance(password, str) or not hmac.compare_digest(password, LEADER_PASSWORD):
            self.send_json(401, {"error": "Senha incorreta."})
            return
        session_id = secrets.token_urlsafe(32)
        with STATE_LOCK:
            LEADER_SESSIONS.add(session_id)
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"leader_session={session_id}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def save_response(self, payload):
        browser_id_hash = device_hash(self)
        answers = payload.get("answers")
        name = payload.get("name")
        crm = payload.get("crm")
        health_plan = payload.get("health_plan")
        custom_plan = payload.get("custom_plan", "")
        consent = payload.get("consent")
        if not browser_id_hash:
            self.send_json(400, {"error": "Cookie de registro do dispositivo ausente. Recarregue a página."})
            return
        if not isinstance(name, str) or not 2 <= len(name) <= 120:
            self.send_json(400, {"error": "Informe um nome válido."})
            return
        if not isinstance(crm, str) or not 1 <= len(crm.strip()) <= 30:
            self.send_json(400, {"error": "Informe um CRM válido."})
            return
        crm = " ".join(crm.upper().split())
        if not isinstance(health_plan, str) or health_plan not in HEALTH_PLANS:
            self.send_json(400, {"error": "Selecione um plano de saúde válido."})
            return
        if not isinstance(custom_plan, str) or len(custom_plan) > 80:
            self.send_json(400, {"error": "Nome do plano personalizado inválido."})
            return
        if health_plan == "Outro plano":
            if not 2 <= len(custom_plan.strip()) <= 80:
                self.send_json(400, {"error": "Informe o nome do outro plano de saúde."})
                return
            health_plan = custom_plan.strip()
        elif custom_plan:
            self.send_json(400, {"error": "Campo de plano personalizado inesperado."})
            return
        if consent is not True:
            self.send_json(400, {"error": "O consentimento é obrigatório para salvar os dados."})
            return
        if not isinstance(answers, dict) or set(answers) != QUESTION_IDS:
            self.send_json(400, {"error": "É necessário responder às nove perguntas."})
            return
        if any(not isinstance(answers[key], str) or answers[key] not in OPTIONS_BY_ID[key] for key in QUESTION_IDS):
            self.send_json(400, {"error": "Uma ou mais opções são inválidas."})
            return

        with STATE_LOCK:
            responses = read_jsonl()
            if any(item.get("browser_id_hash") == browser_id_hash for item in responses):
                self.send_json(409, {"error": "Este dispositivo já registrou uma resposta."})
                return
            response = {
                "submitted_at": utc_now(),
                "browser_id_hash": browser_id_hash,
                "name": name,
                "crm": crm,
                "health_plan": health_plan,
                "answers": answers,
            }
            with RESPONSES_FILE.open("a", encoding="utf-8") as file:
                file.write(json.dumps(response, ensure_ascii=False) + "\n")
            rebuild_script(responses + [response], read_slots())
        self.send_json(201, {"ok": True})

    def update_slot(self, payload):
        slot = payload.get("slot")
        if not isinstance(slot, int) or slot < 1 or slot > 10 or payload.get("status") != "eliminada":
            self.send_json(400, {"error": "Slot inválido."})
            return
        with STATE_LOCK:
            slots = read_slots()
            slots[str(slot)] = {"status": "eliminada", "updated_at": utc_now()}
            atomic_write_json(SLOTS_FILE, slots)
            rebuild_script(read_jsonl(), slots)
        self.send_json(200, {"ok": True, "slot": slot})


def main():
    ensure_storage()
    with STATE_LOCK:
        rebuild_script(read_jsonl(), read_slots())
    server = ThreadingHTTPServer((HOST, PORTA), AppHandler)
    print(f"Servidor de validação ativo em http://localhost:{PORTA}")
    print(f"Link local para respostas: http://localhost:{PORTA}/entrevista")
    print(f"Painel administrativo local: http://localhost:{PORTA}/admin")
    print(f"URL configurada para formulário: {PUBLIC_FORM_URL}")
    print(f"URL configurada para admin: {ADMIN_URL}")
    print(f"Link alternativo protegido: http://localhost:{PORTA}/?leader={leader_token()}")
    print("No Codespaces, torne a porta 8080 pública para compartilhar o link.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
