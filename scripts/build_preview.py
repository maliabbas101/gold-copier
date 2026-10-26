"""Export the real frontend with read-only sample data; no secrets or broker access."""

import argparse
import json
from pathlib import Path
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--data")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    web = root / "goldcopier/web"
    if args.data:
        bundle = json.loads(Path(args.data).read_text())
        sample = bundle["sample"]
        config = bundle["config"]
    else:
        with urlopen(args.url + "/api/demo") as response:
            sample = json.load(response)
        with urlopen(args.url + "/api/config") as response:
            config = json.load(response)
    if not config["demo"]:
        raise ValueError("Only export from a seeded demo workspace")
    app = (web / "app.js").read_text()
    start = app.index("async function api(")
    end = app.index("\nconst post=", start)
    adapter = (
        'async function api(path,options={}) { if(options.method && options.method!=="GET") throw Error("Read-only preview. Start the API and sign in to edit settings."); if(path==="/config") return '
        + json.dumps(config)
        + '; if(path==="/demo") return '
        + json.dumps(sample)
        + '; if(path==="/dashboard") return '
        + json.dumps(sample["dashboard"])
        + '; throw Error("Sign in through the running API to continue."); }'
    )
    app = app[:start] + adapter + app[end:]
    import base64

    mark = (
        "data:image/svg+xml;base64,"
        + base64.b64encode((web / "mark.svg").read_bytes()).decode()
    )
    app = app.replace("/assets/mark.svg", mark).replace("</script", "<\\/script")
    html = (
        (web / "index.html")
        .read_text()
        .replace('<link rel="icon" href="/assets/mark.svg" type="image/svg+xml">', "")
    )
    html = html.replace(
        '<link rel="stylesheet" href="/assets/styles.css">',
        "<style>" + (web / "styles.css").read_text() + "</style>",
    )
    html = html.replace(
        '<script src="/assets/app.js" type="module"></script>',
        '<script type="module">' + app + "</script>",
    )
    output = root / "docs/frontend-preview.html"
    output.write_text(html)
    print(output)


if __name__ == "__main__":
    main()
