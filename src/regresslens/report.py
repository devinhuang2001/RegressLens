from __future__ import annotations

import html
import json
import re
import shlex
from pathlib import Path

from .profiling import get_profile
from .storage import Store

WEB = Path(__file__).parent / "web"


def report_data(store: Store, inv: dict) -> dict:
    reproduction = shlex.join(["regresslens", "--store", str(store.root), "investigate",
                               "--repo", inv["repository"], "--good", inv["good"],
                               "--bad", inv["bad"], "--config",
                               str(store.path(inv["id"]) / "config.json")])
    if inv["config"]["backend"] == "guidellm":
        reproduction += " --trusted-repo"
    if (inv["case_type"] == "controlled_fixture"
            and inv["id"] in ("regression", "stable", "noisy", "environment")):
        reproduction = ("python demo/fixture.py --out .regresslens/reproduction --case "
                        + inv["id"])
    try:
        profiling = get_profile(store, inv)
        profile_warning = None
    except (OSError, ValueError):
        profiling = None
        profile_warning = "The linked profiling manifest could not be read."
    if profiling and profiling["source"] == "synthetic_fixture":
        reproduction = "python demo/profiling_fixture.py --out .regresslens/profiling-demo"
    return {**inv, "reproduction": reproduction, "profiling": profiling, "profiling_warning": profile_warning,
            "artifact_root": str(store.path(inv["id"])),
            "measurement_note": ("Controlled synthetic fixture" if inv["case_type"] == "controlled_fixture"
                                 else "Measured benchmark runs"),
            "method": ("Alternating paired rounds. Percentile bootstrap of mean per-pair relative "
                       "degradation. Positive values indicate worse performance. One prespecified "
                       "gate determines the investigation result. Auxiliary intervals are descriptive.")}


def pct(number):
    return f"{100 * number:+.1f}%" if number is not None else "Unavailable"


def markdown(data: dict) -> str:
    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")

    gate = data["gate"] or {}
    localized = data["localization"] or {}
    lines = [
        f"# RegressLens investigation {data['id']}", "",
        f"**{data['case_type']} · {data['state']} · {gate.get('decision', 'PENDING')}**", "",
        data["measurement_note"], "", data["method"], "",
        "## Observations", "",
        f"- Reference commit `{data['good']}`",
        f"- Candidate commit `{data['bad']}`",
        f"- Gate workload `{data['config']['gate_scenario']}` and metric `{data['config']['gate_metric']}`",
        f"- Model `{cell(data['config']['model'])}` at `{cell(data['config']['model_revision'])}`",
        f"- Tokenizer `{cell(data['config']['tokenizer'])}` at `{cell(data['config']['tokenizer_revision'])}`",
        "",
        "| Workload | Metric | Relative degradation | Confidence interval | Pairs | Decision |",
        "|---|---|---:|---|---:|---|",
    ]
    if data.get("error"):
        lines[8:8] = [f"- Execution detail {cell(data['error'])}"]
    for row in data["metrics"]:
        interval = " to ".join(map(pct, row["interval"])) if row["interval"] else "Unavailable"
        lines.append(f"| {cell(row['scenario'])} | {cell(row['metric'])} | {pct(row['delta'])} | "
                     f"{interval} | {row['pairs']} | {row['decision']} |")
    if gate.get("warnings"):
        lines.extend(["", *[f"- {cell(warning)}" for warning in gate["warnings"]]])
    lines.extend(["", "## Commit localization", ""])
    for key, value in localized.items():
        lines.append(f"- **{cell(key)}** {cell(value)}")
    for decision in data["decisions"]:
        detail = decision.get("reason") or pct((decision.get("gate") or {}).get("delta"))
        lines.append(f"- `{decision['sha'][:12]}` **{decision['status']}** · {cell(detail)}")
    lines.extend(["", "## Hypotheses and counterevidence", ""])
    for row in data["findings"]:
        lines.extend([f"### {row['category']} · {row['verification']}", "", cell(row["summary"]), ""])
        for label, key in [("Supporting observation", "support"), ("Counterevidence", "against"),
                           ("Missing", "missing"), ("Related source", "files")]:
            for item in row[key]:
                if isinstance(item, dict):
                    detail = (f"{item['scenario']} / {item['metric']} · {item['baseline']:.4g} → "
                              f"{item['candidate']:.4g} · {pct(item['relative_delta'])} · "
                              f"runs {', '.join(item['baseline_ids'] + item['candidate_ids'])}")
                else:
                    detail = str(item)
                lines.append(f"- **{label}** {cell(detail)}")
        lines.extend([f"- **Proposed experiment** {cell(row['next'])}", ""])
    if not data["findings"]:
        lines.append("No hypothesis reached the diagnostic trigger.")
    lines.extend(["", "## Independent verification", ""])
    for verification in data["verifications"]:
        lines.append(f"- **{verification['status']}** · {cell(verification['hypothesis'])} · "
                     f"{cell(verification.get('commit') or '')} · {cell(verification['reason'])}")
        for evidence in verification.get("evidence", []):
            if isinstance(evidence, dict) and "metric" in evidence:
                bounds = " to ".join(map(pct, evidence["interval"])) if evidence["interval"] else "Unavailable"
                lines.append(f"  - {cell(evidence['metric'])} · {pct(evidence['delta'])} · CI {bounds} · "
                             f"{evidence['pairs']} pairs")
    if not data["verifications"]:
        lines.append("No independent intervention is recorded.")
    if profile := data.get("profiling"):
        lines.extend(["", "## Independent profiling", "",
                      f"- Workload {cell(profile['scenario'])}",
                      f"- Good `{profile['commits']['good']}` · Bad `{profile['commits']['bad']}`",
                      f"- Provenance {cell(profile['source'])}",
                      "- Profiling evidence is separate from regression-gate observations."])
        for kind, artifact in profile["artifacts"].items():
            lines.append(f"- {cell(kind)} · `profiles/{cell(artifact['name'])}`")
        kernels = profile.get("kernels", [])
        if kernels and profile.get("kernel_comparable"):
            lines.extend(["", "| Kernel | Good summed ms | Bad summed ms | Delta ms |", "|---|---:|---:|---:|"])
            for row in kernels[:15]:
                good = (row["good"] or {}).get("total_us", 0) / 1000
                bad = (row["bad"] or {}).get("total_us", 0) / 1000
                lines.append(f"| {cell(row['name'])} | {good:.3f} | {bad:.3f} | {bad-good:+.3f} |")
        lines.extend(f"- {cell(warning)}" for warning in profile.get("warnings", []))
    lines.extend(["", "## Reproduction", "", "```sh", data["reproduction"], "```", "",
                  f"Artifact root `{cell(data['artifact_root'])}`", "",
                  "### Workload and environment", "", "```json",
                  json.dumps(data["config"]["scenarios"], indent=2), "```", ""])
    environment = next(iter(data["experiments"].values()), {}).get("environment")
    if environment:
        lines.extend(["```json", json.dumps(environment, indent=2), "```", ""])
    lines.extend(["### Raw artifacts", ""])
    for experiment in data["experiments"].values():
        names = ", ".join(cell(path) for path in experiment["artifacts"].values())
        lines.append(f"- `{cell(experiment['id'])}` · {names}")
    return "\n".join(lines) + "\n"


def standalone(data: dict) -> str:
    page = (WEB / "index.html").read_text()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", r"\u003c")
    markdown_payload = json.dumps(markdown(data), ensure_ascii=False).replace("<", r"\u003c")
    blob = (f'<script type="application/json" id="investigation-data">{payload}</script>'
            f'<script type="application/json" id="investigation-markdown">{markdown_payload}</script>')
    page = re.sub(r'<script\b[^>]*src="([^"]+)"[^>]*></script>',
                  lambda match: '<script type="module">' +
                  (WEB / match[1].lstrip("./")).read_text().replace("</script", r"<\/script") +
                  "</script>", page)
    page = re.sub(r'<link\b[^>]*href="([^"]+\.css)"[^>]*>',
                  lambda match: "<style>" + (WEB / match[1].lstrip("./")).read_text() + "</style>", page)
    return page.replace('<div id="root"></div>', blob + '<div id="root"></div>')


def render(store: Store, inv: dict, format: str) -> str:
    data = report_data(store, inv)
    if format == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)
    if format in ("md", "markdown"):
        return markdown(data)
    if format == "html":
        return standalone(data)
    raise ValueError("Use html, json, or markdown")
