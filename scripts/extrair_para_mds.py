#!/usr/bin/env python3
"""Extrai os Markdown do endpoint protegido ou usa o JSONL local."""

import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATH = ROOT / "docs" / "ROTEIRO-ENTREVISTA.md"
INDIVIDUALS_PATH = ROOT / "docs" / "pesquisas"
QUESTION_TITLES = {
    "perfil": "Qual é o seu perfil nesta validação?",
    "volume": "Quantas glosas sua operação analisa em um mês típico?",
    "tempo": "Quanto tempo a equipe dedica à análise de cada glosa?",
    "abandono": "No último trimestre, houve glosas abandonadas por falta de tempo ou informação?",
    "retrabalho": "Com que frequência a equipe refaz uma análise por falta de contexto?",
    "visibilidade": "Como você acompanha o motivo das glosas?",
    "prioridade": "Qual seria o maior ganho de uma ferramenta de validação?",
    "interesse": "Você participaria de um teste online da solução?",
    "dor": "Qual frase descreve melhor a dor da sua operação?",
}


def write_exports(payload):
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text(payload["consolidado_md"], encoding="utf-8")
    INDIVIDUALS_PATH.mkdir(parents=True, exist_ok=True)
    for existing in INDIVIDUALS_PATH.glob("entrevista_*.md"):
        existing.unlink()
    for path, content in payload["arquivos_individuais"].items():
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def load_local():
    source = ROOT / "dados" / "respostas_clinicas.jsonl"
    responses = []
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            if line.strip():
                responses.append(json.loads(line))
    return responses


def slugify(value):
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    value = str(value).lower().replace(" ", "-")
    return "".join(character for character in value if character in allowed).strip("-") or "respondente"


def local_payload(responses):
    individual_files = {}
    summaries = ["# Roteiro de Entrevista — Validação de Mercado", "", "## Resumo das entrevistas", ""]
    for index, response in enumerate(responses, 1):
        answers = response.get("answers", {})
        persona = answers.get("perfil", "—")
        lines = [
            f"# Entrevista #{index:02d}",
            "",
            f"- **Data:** {response.get('submitted_at', '—')}",
            f"- **Nome:** {response.get('name', '—')}",
            f"- **CRM:** {response.get('crm', '—')}",
            f"- **Plano de saúde:** {response.get('health_plan', '—')}",
            "",
            "## Respostas",
            "",
        ]
        for number, (key, title) in enumerate(QUESTION_TITLES.items(), 1):
            lines.extend([f"### {number}. {title}", "", f"> {answers.get(key, '—')}", ""])
        relative = f"docs/pesquisas/entrevista_{index:02d}.md"
        individual_files[relative] = "\n".join(lines)
        summaries.extend(
            [
                f"### Entrevista #{index:02d} — {response.get('name', '—')}",
                "",
                f"- **Persona:** {persona}",
                f"- **CRM:** {response.get('crm', '—')}",
                f"- **Plano:** {response.get('health_plan', '—')}",
                f"- **Dor:** {answers.get('dor', '—')}",
                "",
            ]
        )
    return {
        "consolidado_md": "\n".join(summaries),
        "arquivos_individuais": individual_files,
        "respostas_raw": responses,
    }


def main():
    if len(sys.argv) > 1:
        base_url = sys.argv[1].rstrip("/")
        request = Request(base_url + "/api/exportar-mds", headers={"Accept": "application/json"})
        if len(sys.argv) > 2:
            request.add_header("Cookie", "leader_session=" + sys.argv[2])
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    else:
        payload = local_payload(load_local())
    write_exports(payload)
    print(f"Consolidado salvo em {EXPORT_PATH}")
    print(f"Entrevistas individuais: {len(payload['arquivos_individuais'])}")


if __name__ == "__main__":
    main()
