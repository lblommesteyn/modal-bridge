"""`modal-bridge`: deploy the gateway into your Modal workspace, then call it."""
import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ENV_FILE = ".modal-bridge.env"


def fail(message):
    raise SystemExit(f"error: {message}")


def read_env(path):
    if not os.path.exists(path):
        return {}
    with open(path) as file:
        return dict(line.rstrip("\n").split("=", 1) for line in file if "=" in line and not line.startswith("#"))


def write_private(path, values):
    """Owner-only file; secrets are written here and never printed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.writelines(f"{key}={value}\n" for key, value in values.items())
    os.chmod(path, 0o600)


def parse_target(text):
    """alias=app/function, or app/function (the alias defaults to the function name)."""
    alias, _, path = text.rpartition("=")
    app_name, slash, function = path.partition("/")
    if not slash or not app_name or not function:
        fail(f"--target expects [alias=]app/function, got {text!r}")
    return alias or function, [app_name, function]


def prompt_targets():
    print("Which deployed Modal functions should the gateway expose?")
    targets = {}
    while True:
        app_name = input("  Modal app name (blank to finish): ").strip()
        if not app_name:
            return targets
        function = input("  Function name: ").strip()
        alias = input(f"  Alias callers will use [{function}]: ").strip() or function
        targets[alias] = [app_name, function]


def request(env, path, method="GET", body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    call = urllib.request.Request(env["BRIDGE_URL"].rstrip("/") + path, data=data, method=method, headers={
        "Authorization": f"Bearer {env['BRIDGE_TOKEN']}", "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {"detail": error.reason}


def deploy(args):
    import modal
    env = {} if args.rotate else read_env(args.env_file)
    targets = dict(parse_target(text) for text in args.target)
    if not targets and env.get("BRIDGE_TARGETS"):
        targets = json.loads(env["BRIDGE_TARGETS"])
    if not targets:
        if not sys.stdin.isatty():
            fail("pass at least one --target [alias=]app/function")
        targets = prompt_targets()
    if not targets:
        fail("no functions selected")
    try:
        for alias, (app_name, function) in targets.items():
            modal.Function.from_name(app_name, function).hydrate()
            print(f"  found {app_name}/{function}  ->  alias {alias!r}")
    except modal.exception.AuthError:
        fail("Modal is not authenticated; run `modal setup` first")
    except modal.exception.NotFoundError:
        fail(f"Modal function {app_name}/{function} is not deployed; run `modal deploy` for that app first")

    values = {
        "BRIDGE_TOKEN": env.get("BRIDGE_TOKEN") or secrets.token_urlsafe(32),
        "BRIDGE_WEBHOOK_SECRET": env.get("BRIDGE_WEBHOOK_SECRET")
                                 or "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "BRIDGE_TARGETS": json.dumps(targets, separators=(",", ":")),
    }
    for option, name in (("callback_hosts", "BRIDGE_CALLBACK_HOSTS"), ("owner_max_active", "BRIDGE_OWNER_MAX_ACTIVE"),
                         ("owner_max_daily", "BRIDGE_OWNER_MAX_DAILY")):
        if getattr(args, option) is not None:
            values[name] = str(getattr(args, option))
    if "BRIDGE_TOKEN" not in env and not args.rotate:
        try:
            modal.Secret.from_name(args.name).hydrate()
        except modal.exception.NotFoundError:
            pass
        else:
            fail(f"a Modal Secret named {args.name!r} already exists but {args.env_file} does not. "
                 "Deploying would replace its token. Re-run with --rotate to do that, or --name to deploy separately.")
    modal.Secret.objects.delete(args.name, allow_missing=True)
    modal.Secret.objects.create(args.name, values)
    print(f"  stored gateway configuration in Modal Secret {args.name!r}")

    os.environ["MODAL_BRIDGE_APP"] = args.name
    from . import modal_host
    with modal.enable_output():
        modal_host.app.deploy()
    url = modal.Function.from_name(args.name, "gateway").get_web_url()
    write_private(args.env_file, {"BRIDGE_URL": url, **values})

    healthy = False
    for _ in range(20):
        try:
            with urllib.request.urlopen(url + "/health", timeout=30) as response:
                healthy = response.status == 200
                break
        except Exception:
            time.sleep(2)
    first = next(iter(targets))
    print(f"\nGateway {'is live' if healthy else 'deployed (health check did not answer yet)'}: {url}")
    print(f"Token and webhook secret saved to {args.env_file} (owner-only; keep it out of git).")
    print(f"\nRun a job:        modal-bridge call {first} '<json input>'")
    print("Worker secrets:   modal-bridge worker-secrets <worker dir>          (wrangler secret put)")
    print("Local wrangler:   modal-bridge worker-secrets <worker dir> --local  (writes .dev.vars)")
    print(f"API docs:         {url}/docs")
    print(f"Remove it:        modal app stop {args.name}")


def call(args):
    env = read_env(args.env_file)
    if "BRIDGE_URL" not in env:
        fail(f"{args.env_file} not found; run `modal-bridge deploy` first or pass --env-file")
    try:
        payload = json.loads(args.input)
    except ValueError:
        fail("input must be JSON, for example '{\"value\": 12}'")
    started = time.monotonic()
    status, job = request(env, "/v1/jobs", "POST", {"function": args.function, "input": payload},
                          {"Idempotency-Key": args.key or f"cli-{uuid.uuid4()}"})
    if status != 202:
        fail(f"HTTP {status}: {job.get('detail')}")
    print(f"job {job['id']}  {job['status']}", file=sys.stderr)
    while job["status"] in ("pending", "submitting", "cancel_requested"):
        if time.monotonic() - started > args.timeout:
            fail(f"still {job['status']} after {args.timeout}s; the job keeps running. Check it later by ID.")
        time.sleep(1)
        status, latest = request(env, f"/v1/jobs/{job['id']}")
        if status == 200:
            job = latest
        elif status not in (502, 503):
            fail(f"HTTP {status}: {latest.get('detail')}")
    print(f"{job['status']} in {time.monotonic() - started:.1f}s", file=sys.stderr)
    print(json.dumps(job.get("result") if job["status"] == "succeeded" else job, indent=2))
    if job["status"] != "succeeded":
        raise SystemExit(1)


def worker_secrets(args):
    env = read_env(args.env_file)
    if "BRIDGE_TOKEN" not in env:
        fail(f"{args.env_file} not found; run `modal-bridge deploy` first or pass --env-file")
    names = ("BRIDGE_TOKEN", "BRIDGE_WEBHOOK_SECRET")
    if args.local:
        path = os.path.join(args.directory, ".dev.vars")
        write_private(path, {**read_env(path), "BRIDGE_URL": env["BRIDGE_URL"], **{name: env[name] for name in names}})
        print(f"Wrote {path} (git-ignored by Wrangler's default template; never commit it).")
        return
    for name in names:
        # Piped through stdin so the value never appears in a process list or the terminal.
        done = subprocess.run(["npx", "wrangler", "secret", "put", name], cwd=args.directory, input=env[name], text=True)
        if done.returncode:
            fail(f"wrangler could not store {name}")
    print(f"Set BRIDGE_URL to {env['BRIDGE_URL']} in the Worker's wrangler config `vars`.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="modal-bridge", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    d = commands.add_parser("deploy", help="deploy or update the gateway on Modal")
    d.add_argument("--target", action="append", default=[], metavar="[alias=]app/function",
                   help="a deployed Modal function to expose; repeatable")
    d.add_argument("--name", default="modal-bridge", help="Modal app and Secret name (default: modal-bridge)")
    d.add_argument("--env-file", default=ENV_FILE)
    d.add_argument("--rotate", action="store_true", help="generate a new token and webhook secret")
    d.add_argument("--callback-hosts", help="comma-separated hosts allowed as callback targets, e.g. '*.workers.dev'")
    d.add_argument("--owner-max-active", type=int, help="unfinished jobs allowed per Bridge-Owner")
    d.add_argument("--owner-max-daily", type=int, help="jobs allowed per Bridge-Owner per 24 hours")
    d.set_defaults(run=deploy)

    c = commands.add_parser("call", help="submit one job and wait for its result")
    c.add_argument("function", help="function alias")
    c.add_argument("input", help="JSON input")
    c.add_argument("--key", help="idempotency key (default: random)")
    c.add_argument("--timeout", type=float, default=600)
    c.add_argument("--env-file", default=ENV_FILE)
    c.set_defaults(run=call)

    w = commands.add_parser("worker-secrets", help="give a Cloudflare Worker the gateway token and webhook secret")
    w.add_argument("directory", help="the Worker project directory")
    w.add_argument("--local", action="store_true", help="write .dev.vars for `wrangler dev` instead of deploying secrets")
    w.add_argument("--env-file", default=ENV_FILE)
    w.set_defaults(run=worker_secrets)

    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
