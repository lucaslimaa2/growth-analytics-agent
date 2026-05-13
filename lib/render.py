"""Dev helper: render a Plotly chart spec as a standalone HTML file.

Used to preview charts produced by tools.output.make_chart while we don't yet
have a real frontend (Phase 9 brings that). Not part of the production code
path; nothing on Vercel imports this.

Usage from a REPL or script:
    from lib.render import save_chart_html
    save_chart_html(chart_spec, "out/preview.html")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <script src="https://cdn.plot.ly/plotly-3.0.0.min.js"></script>
  <style>
    body {{ font-family: Inter, system-ui, sans-serif; margin: 24px; }}
    #chart {{ width: 900px; height: 500px; }}
  </style>
</head>
<body>
  <h2>{title}</h2>
  <div id="chart"></div>
  <script>
    const spec = {spec};
    Plotly.newPlot('chart', spec.data, spec.layout, {{ responsive: true }});
  </script>
</body>
</html>
"""


def save_chart_html(chart_result: dict[str, Any], out_path: str | Path) -> Path:
    """Write a self-contained HTML preview of a make_chart result.

    `chart_result` is the dict returned by tools.output.make_chart.
    Open the resulting file in any browser.
    """
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    html = _TEMPLATE.format(
        title=chart_result.get("title", "Chart"),
        spec=json.dumps(chart_result.get("spec", {})),
    )
    p.write_text(html, encoding="utf-8")
    return p
