"""Re-render the JSON examples in an FE integration guide from its capture files.

Each example in the guide is preceded by a directive comment naming the capture it comes from:

    <!-- capture: 075-a-bookings-new-requests.json response.body first=1 -->
    ```json
    ...rewritten from the capture...
    ```

Parts: ``request.body``, ``response.body`` or ``observed`` (whole file minus ``note``).
Options: ``first=N`` keeps the first N list items; ``keys=a,b,c`` keeps only those keys of an
object (for long objects); ``path=x.y`` descends into the part first.

    poetry run python -m scripts.render_fe_guide docs/fe-integration-guide-caregiver.md docs/fe-captures/caregiver
    poetry run python -m scripts.render_fe_guide ... --check   # exit 1 if the guide is stale

Never edit an example by hand: re-capture, then re-render.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DIRECTIVE = re.compile(
    r"(?P<indent>[ ]*)<!-- capture: (?P<file>\S+) (?P<part>\S+)(?P<opts>(?: \w+=\S+)*) -->\n"
    r"(?P=indent)```json\n.*?\n(?P=indent)```",
    re.S,
)


def _select(capture: dict, part: str, opts: dict) -> Any:
    if part == "observed":
        value: Any = {k: v for k, v in capture.items() if k != "note"}
    else:
        value = capture
        for key in part.split("."):
            value = value[key]
    for key in opts.get("path", "").split(".") if opts.get("path") else []:
        value = value[int(key)] if isinstance(value, list) else value[key]
    if "first" in opts and isinstance(value, list):
        value = value[: int(opts["first"])]
    if "keys" in opts and isinstance(value, dict):
        keep = opts["keys"].split(",")
        value = {k: value[k] for k in keep if k in value}
    return value


def render(guide: str, captures_dir: Path) -> str:
    def replace(m: re.Match) -> str:
        opts = dict(o.split("=", 1) for o in m.group("opts").split())
        capture = json.loads((captures_dir / m.group("file")).read_text())
        body = json.dumps(_select(capture, m.group("part"), opts), indent=2, ensure_ascii=False)
        indent = m.group("indent")
        body = "\n".join(indent + line for line in body.splitlines())
        head = m.group(0).split("\n", 1)[0]
        return f"{head}\n{indent}```json\n{body}\n{indent}```"

    return DIRECTIVE.sub(replace, guide)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("guide", type=Path)
    p.add_argument("captures", type=Path)
    p.add_argument("--check", action="store_true", help="exit 1 if re-rendering would change the guide")
    args = p.parse_args(argv)
    current = args.guide.read_text()
    rendered = render(current, args.captures)
    if args.check:
        if rendered != current:
            print(f"{args.guide} is out of date with {args.captures}; run scripts.render_fe_guide", file=sys.stderr)
            return 1
        return 0
    args.guide.write_text(rendered)
    print(f"rendered {len(DIRECTIVE.findall(current))} examples in {args.guide}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
