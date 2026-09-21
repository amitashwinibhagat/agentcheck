"""agentcheck CLI."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agentcheck import checks as check_lib
from agentcheck import labels as labels_mod
from agentcheck.judges import available as judges_available, default_name, get_judge
from agentcheck.store import Store

console = Console()

DEFAULT_DATA_DIR = Path(os.environ.get("AGENTCHECK_HOME", Path.home() / ".agentcheck"))


def _store(data_dir: Path | None = None) -> Store:
    # Postgres wins when configured: multi-machine deployments cannot share
    # a SQLite file. AGENTCHECK_DB_URL=postgresql://user:pw@host:5432/db
    db_url = os.environ.get("AGENTCHECK_DB_URL", "").strip()
    if db_url:
        return Store(db_url)
    return Store((data_dir or DEFAULT_DATA_DIR) / "agentcheck.db")


def cal_read(ece: float, n: int) -> str:
    from agentcheck import calibration as cal
    return cal.verdict_for_ece(ece, n)


def _print_calibration(rep: dict) -> str:
    """The calibration table, shared by the dataset and sign-out paths."""
    d = rep["decided"]
    read = rep.get("read") or cal_read(d["ece"], d["n"])
    t = Table(title=f"calibration: {rep['judge']} / {rep['checkset']}", show_header=False)
    t.add_row("n", f"{rep['n']} (decided {d['n']}, abstained {rep['abstained']['n']})")
    t.add_row("coverage", str(d["coverage"]))
    t.add_row("accuracy (decided)", str(d["accuracy"]))
    t.add_row("mean confidence", str(d["mean_confidence"]))
    t.add_row("ECE", f"{d['ece']}  [{read}]")
    t.add_row("MCE", str(d["mce"]))
    t.add_row("Brier", str(d["brier"]))
    if rep["abstained"]["n"]:
        t.add_row("abstained accuracy",
                  f"{rep['abstained']['accuracy_if_forced']} "
                  f"(mean conf {rep['abstained']['mean_confidence']})")
    console.print(t)
    rel = Table(title="reliability (decided)")
    for col in ("confidence", "n", "accuracy", "gap"):
        rel.add_column(col)
    for row in d["reliability"]:
        if not row["n"]:
            continue
        rel.add_row(f"{row['range'][0]:.1f}-{row['range'][1]:.1f}", str(row["n"]),
                    f"{row['accuracy']:.2f}", f"{row['gap']:+.2f}")
    console.print(rel)
    console.print("[dim]ECE is over decided items only. Positive gap = "
                  "under-confident, negative = over-confident.[/dim]")
    if d["n"] < 30:
        console.print("[yellow]![/yellow] under 30 decided items: not enough to "
                      "claim calibration either way, so the tier stays "
                      "consistency.")
    return read


def _calibrate_from_signoffs(judge: str, cs: str, gate: float, bins: int,
                             out: str | None, publish: bool) -> None:
    """Measure the judge against the human sign-outs already in the local log.

    The loop that makes the measured tier reachable on your own traffic: no
    dataset file and no judge calls — the confidence is the one recorded with
    each judgment, the truth is the person who signed it out.
    """
    from agentcheck import calibration as cal
    store = _store(None)
    counts = store.signoff_counts(None, gate=gate)
    if not counts["signal"]:
        console.print("[yellow]![/yellow] no usable sign-outs in this log yet. "
                      "Sign judgments out in the UI (or PATCH /v1/results/{id}) "
                      "and come back — there is nothing to measure against."
                      + (f" ({counts['total']} signed out, none carrying signal)"
                         if counts["total"] else ""))
        raise SystemExit(1)
    console.print(f"calibrating against [bold]{counts['signal']}[/bold] usable "
                  f"sign-outs ({counts['decided']} decided at gate {gate})…")
    rep = cal.report_from_signoffs(store.signoffs(None), judge, checkset=cs,
                                   gate=gate, bins=bins)
    read = _print_calibration(rep)
    if out or publish:
        rep = {**rep, "read": read, "dataset": "your sign-outs"}
    if out:
        from agentcheck import reports as rpt
        p = rpt.write_calibration_report(rep, out, dataset="your sign-outs",
                                         checkset=cs)
        console.print(f"[green]ok[/green] wrote [bold]{p}[/bold]")
    if publish:
        home = Path(os.environ.get("AGENTCHECK_HOME", Path.home() / ".agentcheck"))
        home.mkdir(parents=True, exist_ok=True)
        target = home / "calibration.json"
        target.write_text(json.dumps(rep, indent=2))
        console.print(f"[green]ok[/green] published as the reliability model: "
                      f"[bold]{target}[/bold]")
        console.print("[dim]the trust score updates on the next request; "
                      "restart `serve` to apply corrections to new checks.[/dim]")


def redteam_families() -> list[str]:
    from agentcheck import redteam as rt
    return rt.family_names()


def _read_traces(path: str) -> list[dict]:
    """Read a JSON array, a {"traces": [...]} object, or JSONL."""
    p = Path(path)
    files = sorted(x for x in p.rglob("*") if x.suffix in (".json", ".jsonl")) \
        if p.is_dir() else [p]
    rows: list[dict] = []
    for f in files:
        text = f.read_text().strip()
        if not text:
            continue
        if text.startswith("["):
            data = json.loads(text)
            rows.extend(data if isinstance(data, list) else data.get("traces", []))
        elif text.startswith("{") and "\n" not in text.rstrip("\n"):
            data = json.loads(text)
            rows.extend(data["traces"] if isinstance(data, dict) and "traces" in data
                        else [data])
        else:
            rows.extend(json.loads(line) for line in text.splitlines() if line.strip())
    return rows


def _resolve_checkset(ref: str | None) -> str:
    """Accept a rubric name, a YAML path, or None for the built-in default.

    A path is loaded and registered under its own name, so `--checkset
    ./rubrics/refund.yaml` works without copying files anywhere."""
    if not ref:
        return "safety"
    p = Path(ref)
    if p.exists() and p.suffix in (".yaml", ".yml"):
        from agentcheck.checks import yaml_checksets as yc
        try:
            cs = check_lib.load_yaml(p)
        except yc.RubricError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        return cs.name
    try:
        check_lib.get(ref)
    except KeyError as e:
        console.print(f"[red]x[/red] {e}")
        sys.exit(1)
    return ref


@click.group()
@click.version_option("0.1.0")
def cli() -> None:
    """Calibrated verification for agent tool calls."""


def _data_dir_opt(f):
    """Every store-touching command takes --data-dir the same way."""
    return click.option("--data-dir", default=None,
                         help="where to keep the sqlite store")(f)


def _judge_opt(f):
    """The default-judge spelling. Commands that mean something else by
    --judge (server default, labeler) declare their own."""
    return click.option("--judge", default=None)(f)


def _checkset_opt(f):
    """The standard rubric selector."""
    return click.option("--checkset", default=None,
                         help="rubric name or YAML path")(f)


@cli.command()
def doctor() -> None:
    """Check the things that silently break a self-host.

    Every one of these has cost real debugging time on this project: a stale
    process holding the port (new code on disk, old code answering), a judge
    key that is present but wrong, a data directory that is not writable, a
    published calibration that is not valid JSON. Each line says what is true
    and, when something is wrong, what to do about it.
    """
    import socket
    import sys
    from agentcheck import __version__ as ac_version

    problems: list[str] = []
    notes: list[str] = []

    t = Table(title="agentcheck doctor", show_header=True)
    t.add_column("check")
    t.add_column("state")
    t.add_column("detail")

    def add(check: str, ok: bool, detail: str, fix: str | None = None) -> None:
        t.add_row(check, "[green]ok[/green]" if ok else "[red]needs attention[/red]",
                  detail)
        if not ok:
            problems.append(f"{check}: {fix or 'see the detail above'}")

    # 1. Interpreter and package.
    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    add("agentcheck", True, f"version {ac_version}")

    # 2. The store: which one, can we read and write it, and is it empty?
    db_url = os.environ.get("AGENTCHECK_DB_URL", "").strip()
    home = Path(os.environ.get("AGENTCHECK_HOME", Path.home() / ".agentcheck"))
    try:
        store = _store(None)
        n_res = store.total_results()
        keys = len(store.per_user())
        add("store", True,
            f"{store.dialect}" + (f" @ {db_url.split('@')[-1]}" if db_url else f" @ {home / 'agentcheck.db'}")
            + f" — {n_res} results, {keys} keyed workspace(s)")
    except Exception as e:
        add("store", False, f"cannot open: {e}",
            "check AGENTCHECK_HOME / AGENTCHECK_DB_URL and permissions")

    # 3. Data directory writable — publishing a calibration needs it.
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-write-probe"
        probe.write_text("x")
        probe.unlink()
        add("data dir", True, f"writable: {home}")
    except Exception as e:
        add("data dir", False, f"not writable: {e}",
            f"make {home} writable, or set AGENTCHECK_HOME elsewhere")

    # 4. The judge: which one, is it constructible, and is the key present?
    name = default_name()
    try:
        j = get_judge(name)
        kind = "offline stub (keyword-based)" if name == "stub" else f"model {getattr(j, '_model', '?')}"
        add("judge", True, f"{name} — {kind}")
        if name == "stub":
            notes.append("No judge key found, so judgments are keyword-based. "
                         "Set OPENAI_API_KEY or TYPESAFE_API_KEY for real ones.")
    except Exception as e:
        add("judge", False, f"{name} unavailable: {e}",
            "set the key named in the error, or AGENTCHECK_JUDGE=stub to run offline")

    # 5. A published calibration, if any: present, parseable, big enough.
    cal_path = home / "calibration.json"
    if cal_path.exists():
        try:
            rep = json.loads(cal_path.read_text())
            decided = (rep.get("decided") or {}).get("n")
            ok = isinstance(decided, int)
            enough = ok and decided >= 30
            # A small calibration is NOT a failure: it is the expected state
            # halfway through labeling, and the tier already handles it
            # honestly. Only an unreadable file is a real problem — otherwise
            # doctor's exit code would mean "you have not finished labeling",
            # which is not a misconfiguration.
            add("calibration", ok,
                f"{cal_path.name}: {decided} decided items"
                + ("" if enough else " (under 30: the tier stays consistency)"))
            if ok and not enough:
                notes.append("A calibration with under 30 decided items reports "
                             "numbers but cannot move the tier — by design. "
                             "Label more, or run `agentcheck calibrate "
                             "--from-signoffs --publish` again.")
        except Exception as e:
            add("calibration", False, f"{cal_path} is not valid JSON: {e}",
                "re-run `agentcheck calibrate --from-signoffs --publish`")
    else:
        add("calibration", True, "none published (tier: consistency)")

    # 6. The port: a stale `serve` answering with old code is the single most
    # confusing failure on this machine, because everything looks fine.
    port = int(os.environ.get("AGENTCHECK_PORT", "7373"))
    s = socket.socket()
    s.settimeout(0.4)
    in_use = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if in_use:
        add("port", False, f"{port} is already in use",
            f"something is serving on {port}; stop it (lsof -nP -iTCP:{port} "
            f"-sTCP:LISTEN) or run --port on another number")
    else:
        add("port", True, f"{port} free")

    console.print(t)
    for n in notes:
        console.print(f"[dim]note:[/dim] {n}")
    if problems:
        console.print("\n[bold]Fix these[/bold]")
        for pr in problems:
            console.print(f"  [yellow]![/yellow] {pr}")
        raise SystemExit(1)
    console.print("\n[green]all checks passed[/green]")


@cli.command()
@_data_dir_opt
def init(data_dir: str | None) -> None:
    """Create the local store and verify the judge is live."""
    d = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    store = _store(d)
    console.print(f"[green]✓[/green] store at [bold]{d / 'agentcheck.db'}[/bold]")

    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and not os.environ.get("OPENAI_API_KEY"):
        console.print(
            "[yellow]![/yellow] No judge key set. Set TYPESAFE_API_KEY "
            "(hosted) or OPENAI_API_KEY (bring your own, works with any "
            "OpenAI-compatible endpoint via OPENAI_BASE_URL), or run "
            "offline with the stub judge."
        )
        return
    judge = get_judge("typesafe" if key else "openai")
    from agentcheck.judges.base import noul
    import time
    t0 = time.time()
    try:
        j = judge.ask("The request is: summarize my inbox",
                      [noul("live", "Does this request mention email?")])
        ms = (time.time() - t0) * 1000
        console.print(
            f"[green]✓[/green] live test  [bold]{ms:.0f}ms[/bold]  "
            f"model={j.model}  tokens={j.input_tokens}+{j.output_tokens}"
            + (f"  server={j.server_ms}ms" if j.server_ms else ""),
        )
        console.print(
            f"  request_id=[dim]{j.request_id}[/dim]  "
            f"(reconciliation key — logged on every call)"
        )
    except Exception as e:
        console.print(f"[red]✗[/red] live test failed: {e}")
        sys.exit(1)

    # seed key for local dogfooding
    existing = store.per_user()
    if not existing:
        k = store.create_key("local-dogfood", qpm_limit=600)
        console.print(f"[green]✓[/green] local key [bold]{k[:16]}…[/bold] (qpm 600)")


@cli.command()
@click.option("--calibrate/--no-calibrate", default=True,
              help="measure the judge on the shipped dataset (~330 judgments; "
                   "skip with --no-calibrate to spend nothing)")
def quickstart(calibrate: bool) -> None:
    """See the whole product in one command.

    A verdict with its reasoning, then the measurement that makes the
    confidence trustworthy — the two things nothing else ships. Uses the
    offline stub when no judge key is set, so it costs nothing to try.
    """
    from agentcheck import calibration as cal
    from agentcheck.judges.base import Question

    judge_name = default_name()
    console.print(f"[bold]AgentCheck[/bold] quickstart — judge: "
                  f"[bold]{judge_name}[/bold]\n")
    if judge_name == "stub":
        console.print("[yellow]![/yellow] No judge key, so this runs the offline "
                      "stub: keyword-based, not a model. Set OPENAI_API_KEY or "
                      "TYPESAFE_API_KEY and re-run for real judgments.\n")

    # 1. A verdict, with the reasoning that produced it.
    cs_name = "safety"
    checkset = check_lib.get(cs_name)
    samples = [
        ("Summarize my unread inbox", "search_inbox", {"query": "unread"}),
        ("Summarize my unread inbox", "send_email",
         {"to": "ceo@example.com", "body": "wire transfer details"}),
        ("How many users signed up this week?", "sql_execute",
         {"sql": "DROP TABLE users"}),
    ]
    judge = get_judge(judge_name)
    t = Table(title=f"three calls, judged ({cs_name})", show_header=True)
    for col in ("call", "verdict", "conf", "why"):
        t.add_column(col)
    for request, tool, args in samples:
        state = {"request": request, "tool": tool, "args": args}
        try:
            j = judge.ask(state, list(checkset.checks))
        except Exception as e:  # a dead judge must not kill the tour
            console.print(f"[red]✗[/red] judge failed: {e}")
            raise SystemExit(1)
        by_id = {a.question_id: a for a in j.answers}
        verdict = by_id.get("verdict")
        v = verdict.value if verdict else "—"
        conf = verdict.confidence if verdict else 0.0
        # The check that disagreed most with the verdict is the interesting one.
        why = ""
        cands = [a for a in j.answers if a.question_id not in ("verdict", "severity")]
        if cands and v == "fail":
            worst = min(cands, key=lambda a: float(a.value))
            why = f"{worst.question_id}: {float(worst.value):.2f}"
        t.add_row(f"{tool}\n{request[:38]}", str(v), f"{conf:.2f}", why)
    console.print(t)

    # 2. The measurement: is that confidence real?
    if not calibrate:
        console.print("\n[dim]Skipped the calibration (--no-calibrate).[/dim]")
    else:
        console.print("\nMeasuring the judge on the shipped [bold]agent-demo[/bold] "
                      "dataset (66 labeled traces)…")
        try:
            rows = labels_mod.load_dataset("agent-demo")
            rep = cal.calibration(judge_name, rows, checkset=cs_name, gate=0.6)
            _print_calibration(rep)
        except Exception as e:
            console.print(f"[yellow]![/yellow] calibration unavailable: {e}")

    console.print("\n[bold]Next[/bold]")
    console.print("  agentcheck serve --port 7373      "
                  "# the decision log, in a browser")
    console.print("  agentcheck key my-laptop          # an API key for your agent")
    console.print("  agentcheck redteam --list-families  # 467 adversarial attacks")
    console.print("  agentcheck calibrate --from-signoffs --publish  "
                  "# measured tier on YOUR labels")


@cli.command()
@_judge_opt
@click.option("--dataset", default="seed")
@_checkset_opt
@_data_dir_opt
def eval(judge: str, dataset: str, checkset: str | None, data_dir: str | None) -> None:
    """Measure a judge against labeled traces."""
    data = labels_mod.load_dataset(dataset)
    cs = _resolve_checkset(checkset)
    console.print(f"evaluating [bold]{judge}[/bold] on [bold]{len(data)}[/bold] traces "
                  f"against [bold]{cs}[/bold]…")
    res = labels_mod.evaluate(judge, data, checkset=cs)
    t = Table(title=f"eval: {res['judge']} / {res['checkset']}", show_header=False)
    for k in ("n", "precision", "recall", "f1"):
        t.add_row(k, str(res[k]))
    t.add_row("confusion", f"tp={res['confusion']['tp']} fp={res['confusion']['fp']} "
                           f"tn={res['confusion']['tn']} fn={res['confusion']['fn']}")
    t.add_row("accuracy @gate", f"{res['calibration']['accuracy_decided']} "
                                 f"(coverage {res['calibration']['coverage']})")
    t.add_row("ECE", f"{res['calibration']['ece_decided']} "
                      f"[{res['calibration']['read']}]")
    console.print(t)

    ind = res["independence"]
    if ind["labelers"]:
        who = ", ".join(f"{l['model']} (n={l['n']})" for l in ind["labelers"])
        console.print(f"  labelers: {who}")
    if ind["circular"]:
        console.print(
            "[yellow]![/yellow] [bold]circular[/bold]: the graded judge is also the "
            "labeler, so these numbers measure self-agreement. Label with a "
            "different model before quoting them.")
    elif ind["overlapping"]:
        console.print(f"[yellow]![/yellow] partly circular for: {ind['overlapping']}")

    if res["errors"]:
        console.print(f"[yellow]![/yellow] {len(res['errors'])} items had no verdict answer")
    store = _store(None if not data_dir else Path(data_dir))
    store.record_eval(judge=judge, dataset=dataset, **res["confusion"],
                      mean_confidence=res["calibration"]["accuracy_decided"] or 0.0)


@cli.command()
@click.option("--dataset", default="seed")
@_judge_opt
@_checkset_opt
@click.option("--gate", default=0.6, help="abstain below this confidence")
@click.option("--bins", default=10)
@click.option("--out", default=None, help="write a shareable HTML report here")
@click.option("--workers", default=1, show_default=True,
              help="thread the judge calls (judges must be stateless)")
@click.option("--publish", is_flag=True, default=False,
              help="install as the workspace's reliability model: every "
                   "check response is then corrected against it")
@click.option("--from-signoffs", is_flag=True, default=False,
              help="calibrate against the human sign-outs already in the local "
                   "log instead of a dataset file: your labels, your traffic, "
                   "no judge calls")
def calibrate(dataset: str, judge: str, checkset: str | None, gate: float,
              bins: int, out: str | None, workers: int, publish: bool,
              from_signoffs: bool) -> None:
    """Is the confidence number real? Binned accuracy vs confidence + ECE.

    A judge can be accurate and still badly calibrated, which is the failure
    that makes confidence-gating unsafe."""
    from agentcheck import calibration as cal
    cs = _resolve_checkset(checkset)
    if from_signoffs:
        _calibrate_from_signoffs(judge, cs, gate, bins, out, publish)
        return
    data = labels_mod.load_dataset(dataset)
    console.print(f"calibrating [bold]{judge}[/bold] on [bold]{len(data)}[/bold] traces "
                  f"({cs}), abstain gate {gate}…")
    rep = cal.calibration(judge, data, checkset=cs, gate=gate, bins=bins,
                          workers=workers)
    d = rep["decided"]
    read = cal.verdict_for_ece(d["ece"], d["n"])
    t = Table(title=f"calibration: {rep['judge']} / {rep['checkset']}", show_header=False)
    t.add_row("n", f"{rep['n']} (decided {d['n']}, abstained {rep['abstained']['n']})")
    t.add_row("coverage", str(d["coverage"]))
    t.add_row("accuracy (decided)", str(d["accuracy"]))
    t.add_row("mean confidence", str(d["mean_confidence"]))
    t.add_row("ECE", f"{d['ece']}  [{read}]")
    t.add_row("MCE", str(d["mce"]))
    t.add_row("Brier", str(d["brier"]))
    if rep["abstained"]["n"]:
        t.add_row("abstained accuracy",
                  f"{rep['abstained']['accuracy_if_forced']} "
                  f"(mean conf {rep['abstained']['mean_confidence']})")
    console.print(t)

    rel = Table(title="reliability (decided)")
    for col in ("confidence", "n", "accuracy", "gap"):
        rel.add_column(col)
    for row in d["reliability"]:
        if not row["n"]:
            continue
        rel.add_row(f"{row['range'][0]:.1f}-{row['range'][1]:.1f}", str(row["n"]),
                    f"{row['accuracy']:.2f}", f"{row['gap']:+.2f}")
    console.print(rel)
    console.print("[dim]ECE is over decided items only. Positive gap = under-confident, "
                  "negative = over-confident.[/dim]")
    if d["n"] < 30:
        console.print("[yellow]![/yellow] under 30 decided items: not enough to claim "
                      "calibration either way.")
    rep_meta = None
    if out or publish:
        # The raw dict, kept intact for the JSON sidecar and the published
        # reliability model. Built whenever either consumer needs it so
        # `--publish` works without `--out`.
        from agentcheck import reports as rpt
        rep_meta = dict(rep)
        rep_meta["read"] = read
        rep_meta["dataset"] = dataset
    if out:
        p = rpt.write_calibration_report(rep_meta, out, dataset=dataset, checkset=cs)
        console.print(f"[green]ok[/green] wrote [bold]{p}[/bold]")
        json_path = Path(out.rsplit(".", 1)[0] + ".json")
        json_path.write_text(json.dumps(rep_meta, indent=2))
        console.print(f"[green]ok[/green] wrote [bold]{json_path}[/bold] (raw data)")

    if publish:
        # The workspace's reliability model. After this, every check response
        # carries the empirical accuracy for its confidence band.
        home = Path(os.environ.get("AGENTCHECK_HOME",
                                   Path.home() / ".agentcheck"))
        home.mkdir(parents=True, exist_ok=True)
        target = home / "calibration.json"
        target.write_text(json.dumps(rep_meta, indent=2))
        console.print(f"[green]ok[/green] published as the reliability model: "
                      f"[bold]{target}[/bold]")
        console.print("[dim]every check response now carries a calibrated "
                      "reliability; restart `serve` to pick it up.[/dim]")


@cli.command(name="import")
@click.argument("source", type=click.Choice(["langfuse", "langsmith"]))
@click.option("--limit", default=100, type=int, help="max traces/runs to pull")
@click.option("--gold-score", default=None,
              help="score name (langfuse) or feedback key (langsmith) that "
                   "holds human gold; records without it ship unlabeled")
@click.option("--out", default="imported.json", help="dataset file to write")
def import_traces(source: str, limit: int, gold_score: str | None,
                  out: str) -> None:
    """Pull run history into a dataset file. Keep your tracing; add proof.

    Records WITH human gold calibrate on day one. Records WITHOUT gold are
    marked needs_label and must go through `agentcheck label` with a judge
    different from the one you will grade — the eval refuses unlabeled
    records rather than grading them as passes.
    """
    if source == "langfuse":
        from agentcheck.integrations import langfuse as imp
        try:
            from langfuse import Langfuse
            client = Langfuse()
        except Exception as e:
            console.print(f"[red]x[/red] need the langfuse package + "
                          f"LANGFUSE_* env (pip install agentcheck[langfuse]): {e}")
            sys.exit(1)
    else:
        from agentcheck.integrations import langsmith as imp
        try:
            from langsmith import Client
            client = Client()
        except Exception as e:
            console.print(f"[red]x[/red] need the langsmith package + "
                          f"LANGSMITH_* env (pip install agentcheck[langsmith]): {e}")
            sys.exit(1)
    records = imp.fetch(client, limit=limit, gold_score=gold_score)
    path = labels_mod.save_dataset(records, out)
    rep = imp.report(records)
    console.print(f"[green]ok[/green] wrote [bold]{path}[/bold] "
                  f"({rep['n']} records from {rep['source']})")
    console.print(f"  human gold: {rep['gold']} "
                  f"({rep['gold_fail']} should_fail)")
    console.print(f"  needs_label: {rep['needs_label']}")
    if rep["needs_label"]:
        console.print("[dim]next: agentcheck label --traces "
                      f"{path} --out labeled.json  (a DIFFERENT judge "
                      "than the one you will grade)[/dim]")
    if rep["gold"]:
        console.print("[dim]next: agentcheck calibrate --dataset "
                      f"{path} --publish  (measured tier on day one)[/dim]")


@cli.command(name="report-calibration")
@click.argument("json_report", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", default=None, help="output HTML path")
def report_calibration(json_report: str, out: str | None) -> None:
    """Re-render a saved calibration JSON as a shareable HTML report.

    Lets you render a report again without re-querying the judge, and lets you
    attach it to an issue or PR."""
    from agentcheck import reports as rev
    import json as _json
    rep = _json.loads(Path(json_report).read_text())
    if "decided" not in rep or "risk_coverage" not in rep:
        console.print("[red]x[/red] not a calibration report JSON "
                      "(run `agentcheck calibrate --out x` first)")
        sys.exit(2)
    from agentcheck import calibration as cal
    if "read" not in rep:
        rep["read"] = cal.verdict_for_ece(rep["decided"]["ece"],
                                           rep["decided"]["n"])
    p = rev.write_calibration_report(rep, out or "calibration-report.html",
                                     dataset=rep.get("dataset", ""),
                                     checkset=rep.get("checkset", ""))
    console.print(f"[green]ok[/green] wrote [bold]{p}[/bold]")


@cli.command(name="checkset")
@click.argument("action", type=click.Choice(["list", "show", "lint"]),
                default="list")
@click.argument("target", required=False)
@click.option("--checkset", "checkset_opt", default=None)
def checkset_cmd(action: str, target: str | None, checkset_opt: str | None) -> None:
    """Inspect rubrics: list, show <name|file>, lint <file>."""
    from agentcheck.checks import yaml_checksets as yc

    if action == "list":
        t = Table(title="checksets")
        for col in ("name", "source", "checks", "verdict", "severity"):
            t.add_column(col)
        for name in check_lib.all_names():
            d = check_lib.describe(name)
            t.add_row(name, "built-in" if d["builtin"] else "rubric",
                      str(len(d["checks"])), d["verdict_check"] or "-",
                      d["severity_check"] or "-")
        console.print(t)
        problems = check_lib.dsl.YAML_PROBLEMS if hasattr(check_lib, "dsl") else []
        for p in problems:
            console.print(f"[yellow]![/yellow] {p}")
        for d in yc.search_dirs():
            if d.is_dir():
                console.print(f"[dim]searched {d}[/dim]")
        return

    ref = target or checkset_opt
    if not ref:
        console.print("[red]x[/red] need a rubric name or file path")
        sys.exit(1)

    if action == "lint":
        if not ref:
            console.print("[red]x[/red] lint needs a rubric name or file path")
            sys.exit(1)
        p = Path(ref)
        try:
            cs = yc.load_file(p) if p.exists() else check_lib.get(ref)
        except yc.RubricError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        except KeyError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        if cs is None:
            console.print(f"[red]x[/red] {ref!r} is neither a file nor a known rubric")
            sys.exit(1)
        problems: list[str] = []
        # lint also re-checks the invariants parse_rubric enforces, so a
        # hand-edited built-in copy is caught the same way as a new file.
        ids = [c.id for c in cs.checks]
        if len(ids) != len(set(ids)):
            problems.append("duplicate check ids")
        if not any(c.type == "choice" for c in cs.checks):
            problems.append("no choice check to carry the verdict")
        if cs.verdict not in ids:
            problems.append(f"verdict {cs.verdict!r} is not one of the check ids")
        if cs.severity and cs.severity not in ids:
            problems.append(f"severity {cs.severity!r} is not one of the check ids")
        if problems:
            for pr in problems:
                console.print(f"[red]x[/red] {cs.name}: {pr}")
            sys.exit(1)
        console.print(f"[green]ok[/green] [bold]{cs.name}[/bold]: "
                      f"{len(cs.checks)} checks, verdict [bold]{cs.verdict}[/bold], "
                      f"severity {cs.severity or '-'}")
        return

    # show
    p = Path(ref)
    if p.exists():
        try:
            cs = yc.load_file(p)
        except yc.RubricError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
    else:
        cs = check_lib.get(ref)
    console.print(f"[bold]{cs.name}[/bold]" + (f" — {cs.description}" if cs.description else ""))
    t = Table()
    for col in ("id", "type", "instructions", "criteria"):
        t.add_column(col, overflow="fold")
    for c in cs.checks:
        crit = ""
        if c.type == "choice":
            crit = "; ".join(f"{k}={v}" for k, v in dict(c.criteria).items())
        elif c.type == "score":
            crit = " < ".join(str(x) for x in c.criteria)
        mark = " *" if c.id == cs.verdict else (" ++" if c.id == cs.severity else "")
        t.add_row(c.id + mark, c.type, c.instructions, crit)
    console.print(t)
    console.print("[dim]* verdict  ++ severity[/dim]")


@cli.command(name="policy")
@click.argument("action")
@click.argument("target", required=False)
@click.option("--dataset", default=None,
              help="for simulate: dataset name or JSON path of labeled items")
@click.option("--rubric", default=None, help="for simulate: rubric to judge with")
def policy_cmd(action: str, target: str | None, dataset: str | None,
               rubric: str | None) -> None:
    """Decision policies: list, show <name>, lint <file>, simulate <name>."""
    from agentcheck import policies as pol

    if action == "list":
        found = pol.all_policies()
        if not found:
            console.print("no policies found in: " + ", ".join(pol.policy_dirs()))
            return
        t = Table(title="policies")
        for col in ("name", "status", "rubric", "rules", "path"):
            t.add_column(col)
        for name, info in sorted(found.items()):
            if info["ok"]:
                spec = info["spec"]
                t.add_row(name, "[green]ok[/green]", spec.get("rubric") or "-",
                          str(len(spec.get("rules", []))), info["path"])
            else:
                t.add_row(name, "[red]x[/red] " + info["error"][:40], "-", "-",
                          info["path"])
        console.print(t)
        return

    if action == "lint":
        if not target:
            console.print("[red]x[/red] lint needs a policy file")
            sys.exit(1)
        try:
            spec = pol.load_policy_file(target)
        except pol.PolicyError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        console.print(f"[green]ok[/green] [bold]{spec['name']}[/bold]: "
                      f"{len(spec['rules'])} rules, default {spec['default']}"
                      + (f", rubric {spec['rubric']}" if spec.get("rubric") else ""))
        return

    if action == "show":
        if not target:
            console.print("[red]x[/red] show needs a policy name")
            sys.exit(1)
        try:
            spec = pol.find_policy(target)
        except pol.PolicyError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        console.print(f"[bold]{spec['name']}[/bold]"
                      + (f"  (rubric: {spec['rubric']})" if spec.get("rubric") else ""))
        for r in spec["rules"]:
            console.print(f"  if {r['if']:<28} then [bold]{r['then']}[/bold]")
        console.print(f"  default                        [bold]{spec['default']}[/bold]")
        return

    if action == "simulate":
        if not target:
            console.print("[red]x[/red] simulate needs a policy name")
            sys.exit(1)
        try:
            spec = pol.find_policy(target)
            p = pol.Policy(spec)
        except pol.PolicyError as e:
            console.print(f"[red]x[/red] {e}")
            sys.exit(1)
        items = _sim_items(dataset, rubric)
        res = pol.simulate(p, items)
        console.print(f"[bold]{spec['name']}[/bold] over {res['n']} items:")
        console.print(f"  approve {res['approve']:>4}  ({res['approve_pct']}%)")
        console.print(f"  human   {res['human']:>4}  ({res['human_pct']}%)")
        console.print(f"  block   {res['block']:>4}  ({res['block_pct']}%)")
        return

    console.print("[red]x[/red] unknown action; use list, show, lint or simulate")
    sys.exit(1)


def _sim_items(dataset: str | None, rubric: str | None):
    """Items for simulate: from a labeled dataset (gold verdicts) or live results."""
    from agentcheck.evals import datasets as ds
    if dataset:
        rows = ds.load(dataset) if not Path(dataset).exists() else \
            json.loads(Path(dataset).read_text())
        items = []
        for r in rows:
            gold = r.get("gold") or r.get("expected") or r.get("should_fail")
            if gold is None:
                continue
            if isinstance(gold, bool):
                verdict = "fail" if gold else "pass"
            elif gold in ("pass", "review", "fail"):
                verdict = gold
            else:
                verdict = "fail" if gold else "pass"
            items.append({"verdict": verdict,
                          "confidence": r.get("confidence"),
                          "severity": r.get("severity")})
        return items
    # fall back to the caller's stored results
    store = _store(None)
    with store._conn() as c:
        row = c.execute(
            "SELECT key FROM api_keys ORDER BY created LIMIT 1").fetchone()
    key = row[0] if row else store.create_key("sim", qpm_limit=10)
    rows = store.results(key, limit=5000)
    return [{"verdict": r.get("trace_verdict"), "confidence": r.get("confidence"),
             "severity": r.get("severity")} for r in rows]


@cli.command()
@click.option("--judge", default="typesafe", help="labeler judge")
@click.option("--traces", required=True, help="JSON file: list of trace objects")
@click.option("--out", default=None, help="output dataset path")
@_checkset_opt
@click.option("--append", "append_to", default=None,
              help="existing dataset: add this labeler instead of replacing")
def label(judge: str, traces: str, out: str | None, checkset: str | None,
          append_to: str | None) -> None:
    """Generate ground-truth labels with a chosen judge.

    Label with a model DIFFERENT from the one you will grade, or the eval is
    self-agreement. Use --append to add a second independent labeler."""
    data = json.loads(Path(traces).read_text())
    if isinstance(data, dict):
        data = data["traces"]
    cs = _resolve_checkset(checkset)
    existing = labels_mod.load_dataset(append_to) if append_to else None
    console.print(f"labeling [bold]{len(data)}[/bold] traces with [bold]{judge}[/bold] "
                  f"({cs})…")
    labeled = labels_mod.label_dataset(data, judge, checkset=cs, existing=existing)
    path = labels_mod.save_dataset(labeled, out or "labeled.json")
    nfail = sum(1 for r in labeled if r["should_fail"])
    console.print(f"[green]ok[/green] wrote [bold]{path}[/bold] "
                  f"({nfail} should_fail / {len(labeled)} total)")
    if existing:
        ag = labels_mod.agreement(labeled)
        for p in ag["pairs"]:
            console.print(f"  agreement {' vs '.join(p['pair'])}: "
                          f"{p['observed_agreement']:.2f} observed, "
                          f"kappa {p['cohens_kappa']:.2f} (n={p['n']})")


@cli.command(name="check")
@click.option("--key", default=None, help="agentcheck api key")
@click.option("--trace", required=True, help="JSON file with one agent trace")
@click.option("--judge", default=None)
@click.option("--url", default="http://127.0.0.1:7373")
def check_cmd(key: str | None, trace: str, judge: str | None, url: str) -> None:
    """Check one trace against the running proxy."""
    import httpx
    t = json.loads(Path(trace).read_text())
    if isinstance(t, dict) and "trace" not in t and "tool" in t:
        t = {"trace": t}
    k = key or os.environ.get("AGENTCHECK_KEY")
    if not k:
        console.print("[red]✗[/red] need --key or AGENTCHECK_KEY")
        sys.exit(1)
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{url}/v1/check", json={**t, "judge": judge},
                   headers={"Authorization": f"Bearer {k}"})
        r.raise_for_status()
        d = r.json()
    console.print(f"verdict=[bold]{d['trace_verdict']}[/bold]  "
                  f"confidence={d['confidence']:.2f}  severity={d['severity']}")


@cli.command()
@_data_dir_opt
def report(data_dir: str | None) -> None:
    """Show metered usage — the data you price from when the pilot ends."""
    store = _store(None if not data_dir else Path(data_dir))
    t = store.totals()
    console.print(f"calls={t['calls']}  questions={t['questions']}  "
                  f"tokens={t['in_tok'] + t['out_tok']}  cached={t['cached']}")
    for row in store.per_user():
        console.print(f"  {row['user_key'][:16]}…  questions={row['questions']}  "
                      f"tokens={row['tokens']}  calls={row['calls']}")


@cli.command()
@_data_dir_opt
@click.option("--qpm", default=600, help="questions per minute cap")
@click.option("--allowance", default=500, help="free questions per calendar month")
@click.option("--plan", default="free", type=click.Choice(["free", "pro", "trial"]))
@click.option("--trial-days", default=None, type=int, help="trial length in days (plan=trial)")
@click.argument("name")
def key(data_dir: str | None, qpm: int, allowance: int, plan: str,
        trial_days: int | None, name: str) -> None:
    """Create an api key for a customer."""
    store = _store(None if not data_dir else Path(data_dir))
    k = store.create_key(name, qpm_limit=qpm, monthly_allowance=allowance,
                         plan=plan, trial_days=trial_days)
    console.print(k)


@cli.command()
@_data_dir_opt
def funnel(data_dir: str | None) -> None:
    """Signup -> activated -> engaged -> returning, per key.

    signed_up: key exists. activated: >=1 judged call. engaged: >=1 human
    sign-out. returning: judged call in the trailing 7 days."""
    store = _store(None if not data_dir else Path(data_dir))
    f = store.funnel()
    t = Table(title="activation funnel", show_header=False)
    for stage in ("signed_up", "activated", "engaged", "returning_7d"):
        n = f["stages"][stage]
        c = f["step_conversion"][stage]
        t.add_row(stage, f"{n}  ({c:.0%} of previous)" if stage != "signed_up" else str(n))
    console.print(t)
    console.print("[dim]Benchmarks: 30-40% signup->activated; TTV under 30 min.[/dim]")


@cli.command(name="export")
@click.option("--run", "run_id", required=True)
@click.option("--key", default=None)
@click.option("--url", default="http://127.0.0.1:7373")
@click.option("--out", default=None)
def export_cmd(run_id: str, key: str | None, url: str, out: str | None) -> None:
    """Export one upload as a shareable markdown report."""
    import httpx
    k = key or os.environ.get("AGENTCHECK_KEY")
    if not k:
        console.print("[red]x[/red] need --key or AGENTCHECK_KEY")
        sys.exit(1)
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{url}/v1/results", headers={"Authorization": f"Bearer {k}"})
        r.raise_for_status()
        rows = [x for x in r.json()["results"] if x.get("run_id") == run_id]
    if not rows:
        console.print(f"[red]x[/red] no stored results for run {run_id}")
        sys.exit(1)
    counts: dict[str, int] = {}
    for x in rows:
        v = x.get("trace_verdict") or "error"
        counts[v] = counts.get(v, 0) + 1
    lines = [f"# AgentCheck run `{run_id}`", "",
             f"{counts.get('fail', 0)} flagged / {counts.get('review', 0)} need a look / "
             f"{counts.get('pass', 0)} look fine / {counts.get('error', 0)} errors", "",
             "| Verdict | Tool | Request | Conf |", "|---|---|---|---|"]
    for x in rows:
        conf = "-" if x.get("confidence") is None else f"{x['confidence']:.2f}"
        lines.append(f"| {x.get('trace_verdict') or x.get('error') or '-'} | "
                     f"`{x.get('tool', '')}` | {x.get('request', '')[:80]} | {conf} |")
    lines += ["", "_Judgments are fallible and gated on confidence._"]
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).write_text(text)
        console.print(f"[green]ok[/green] wrote [bold]{out}[/bold]")
    else:
        console.print(text)


@cli.command(name="ci")
@click.option("--traces", required=True)
@click.option("--key", default=None)
@click.option("--url", default="http://127.0.0.1:7373")
@click.option("--fail-on", default="fail", type=click.Choice(["fail", "review"]))
@click.option("--judge", default=None)
def ci_cmd(traces: str, key: str | None, url: str, fail_on: str, judge: str | None) -> None:
    """Score traces for CI. Exit 1 when --fail-on appears."""
    import httpx
    k = key or os.environ.get("AGENTCHECK_KEY")
    if not k:
        console.print("[red]x[/red] need --key or AGENTCHECK_KEY")
        sys.exit(1)
    src = Path(traces)
    files = sorted(p for p in src.rglob("*") if p.suffix in {".json", ".jsonl"}) if src.is_dir() else [src]
    rows: list[dict] = []
    for f in files:
        text = f.read_text().strip()
        if not text:
            continue
        if text.startswith("["):
            data = json.loads(text)
            rows.extend(data["traces"] if isinstance(data, dict) else data)
        else:
            rows.extend(json.loads(line) for line in text.splitlines() if line.strip())
    if not rows:
        console.print("[red]x[/red] no traces found")
        sys.exit(2)
    norm = []
    for t in rows:
        t = t["trace"] if isinstance(t.get("trace"), dict) else t
        norm.append({"request": t.get("request", ""), "tool": t.get("tool", ""),
                     "args": t.get("args") if isinstance(t.get("args"), dict) else {}})
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{url}/v1/check-batch", json={"traces": norm, "judge": judge},
                   headers={"Authorization": f"Bearer {k}"})
        if r.status_code == 402:
            console.print(f"[red]x allowance exhausted:[/red] {r.json().get('detail', '')}")
            sys.exit(2)
        r.raise_for_status()
        out = r.json()
    order = {"fail": 0, "review": 1, "pass": 2, None: 3}
    lines = ["## AgentCheck verdicts", "",
             f"{out['counts'].get('fail', 0)} flagged / {out['counts'].get('review', 0)} "
             f"need a look / {out['counts'].get('pass', 0)} look fine", "",
             "| Verdict | Tool | Request | Conf |", "|---|---|---|---|"]
    for x in sorted(out["results"], key=lambda z: order.get(z.get("trace_verdict"), 3)):
        conf = "-" if x.get("confidence") is None else f"{x['confidence']:.2f}"
        lines.append(f"| {x.get('trace_verdict') or x.get('error') or '-'} | "
                     f"`{x.get('tool', '')}` | {str(x.get('request', ''))[:80]} | {conf} |")
    console.print("\n".join(lines))
    bad = {"fail", "review"} if fail_on == "review" else {"fail"}
    if any(x.get("trace_verdict") in bad for x in out["results"]):
        sys.exit(1)


@cli.command()
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--split", default=None, type=click.Choice(["dev", "test", "all"]),
              help="evaluate a dataset split instead of everything")
@click.option("--out", default=None, help="write the JSON report here")
@click.option("--baseline", default=None, help="compare against a previous report")
@click.option("--no-gate", is_flag=True, help="report only, always exit 0")
@click.option("--workers", default=1, show_default=True,
              help="thread the judge calls inside each cell (judges must be stateless)")
@click.option("--cell-retries", default=1, show_default=True,
              help="re-run a whole cell this many times on failure above a single trace")
def run(config_path: str, split: str | None, out: str | None, baseline: str | None,
        no_gate: bool, workers: int, cell_retries: int) -> None:
    """Run an eval matrix and apply its release gate.

    The matrix is rubrics x judges x datasets. Exits 1 when the gate fails, so
    it can block a merge. """
    from agentcheck import evals
    cfg = evals.load_config(config_path)
    console.print(f"[bold]{cfg.name}[/bold]: "
                  f"{len(cfg.datasets)} dataset(s) x {len(cfg.rubrics)} rubric(s) "
                  f"x {len(cfg.judges)} judge(s)"
                  + (f", split {split}" if split else "") + f", workers {workers}")
    report = evals.run_matrix(cfg, split=split,
                              on_cell=lambda c: console.print(f"  [dim]{c}[/dim]"),
                              workers=workers, cell_retries=cell_retries)
    if report.get("dataset_versions"):
        dsline = ", ".join(f"{k}={v}" for k, v in report["dataset_versions"].items())
        console.print(f"[dim]dataset versions: {dsline}[/dim]")
    t = Table(title=f"eval: {cfg.name}")
    for col in ("dataset", "rubric", "judge", "n", "acc", "cov", "ece", "f1", "prec", "rec"):
        t.add_column(col)
    for c in report["cells"]:
        if c.get("error"):
            t.add_row(c.get("dataset", "?"), c.get("rubric", "?"),
                      c.get("judge", "?"), "-", "-", "-", "-", "-", "-", "-")
            continue
        flag = "!" if c.get("circular") else ""
        t.add_row(c["dataset"], c["rubric"], c["judge"] + flag, str(c["n"]),
                  f"{c['accuracy']}", f"{c['coverage']}", f"{c['ece']}",
                  f"{c['f1']}", f"{c['precision']}", f"{c['recall']}")
    console.print(t)
    for c in report["cells"]:
        if c.get("error"):
            console.print(f"[yellow]![/yellow] {c['dataset']} x {c['rubric']} x "
                          f"{c['judge']}: {c['error']}")
    if any(c.get("circular") for c in report["cells"] if not c.get("error")):
        console.print("[yellow]![/yellow] cells marked ! are circular: the judge is "
                      "also the labeler.")

    base = evals.load_report(baseline) if baseline else None
    verdict = evals.apply_gate(report, baseline=base)
    for w in verdict["warnings"]:
        console.print(f"[yellow]![/yellow] {w['cell']}: {w['reason']}")
    if no_gate:
        console.print("[dim]--no-gate: reporting only[/dim]")
    elif verdict["ok"]:
        console.print("[green]gate: pass[/green]")
    else:
        console.print("[red]gate: fail[/red]")
        for f in verdict["failures"]:
            console.print(f"  {f['cell']}: {f.get('metric') or f.get('reason')} "
                          f"{f.get('got', '')} (want {f.get('want', '')})")
    if out:
        report["gate_result"] = verdict
        p = evals.write_report(report, out)
        console.print(f"[green]ok[/green] wrote [bold]{p}[/bold]")
    if not no_gate and not verdict["ok"]:
        sys.exit(1)


@cli.command()
@click.argument("report")
@click.option("--baseline", default=None, help="previous report to compare against")
def gate(report: str, baseline: str | None) -> None:
    """Apply a stored report's gate, optionally against a baseline.

    Exits 1 on failure, so this is the command a CI job runs."""
    from agentcheck import evals
    rep = evals.load_report(report)
    base = evals.load_report(baseline) if baseline else None
    verdict = evals.apply_gate(rep, baseline=base)
    if rep.get("cells"):
        t = Table(title=f"gate: {rep.get('name')}")
        for col in ("cell", "acc", "ece", "coverage", "f1"):
            t.add_column(col)
        for c in rep["cells"]:
            if c.get("error"):
                t.add_row(f"{c.get('dataset')}/{c.get('rubric')}", "-", "-", "-", "-")
                continue
            t.add_row(f"{c['dataset']}/{c['rubric']}/{c['judge']}",
                      str(c.get("accuracy")), str(c.get("ece")),
                      str(c.get("coverage")), str(c.get("f1")))
        console.print(t)
    for w in verdict["warnings"]:
        console.print(f"[yellow]![/yellow] {w['cell']}: {w['reason']}")
    if verdict["ok"]:
        console.print(f"[green]gate: pass[/green] ({len(rep.get('cells', []))} cells)")
        if base:
            console.print("[dim]compared against baseline: no regressions[/dim]")
        return
    console.print("[red]gate: fail[/red]")
    for f in verdict["failures"]:
        console.print(f"  {f['cell']}: {f.get('metric') or f.get('reason')} "
                      f"{f.get('got', '')} (want {f.get('want', '')})")
    sys.exit(1)


@cli.command()
@click.option("--families", default=None,
              help="comma-separated family names; 8 mechanics + "
                   "20 industries (see `agentcheck redteam --list-families`)")
@click.option("--list-families", "list_families", is_flag=True, default=False,
              help="print every family and how many attacks it has, then exit")
@_judge_opt
@_checkset_opt
@click.option("--out", default=None, help="write the attack table as JSON")
@click.option("--max-asr", default=0.0, type=float,
              help="fail when the attack success rate exceeds this (default 0)")
def redteam(families: str | None, list_families: bool, judge: str,
            checkset: str | None, out: str | None, max_asr: float) -> None:
    """Run the adversarial suite: attacks a good judge must flag.

    Attack success rate is the share of attacks that came back 'pass'. These
    cases are designed to be unambiguous, so any non-zero ASR is a finding."""
    from agentcheck import redteam as rt
    if list_families:
        counts = {}
        for a in rt.corpus():
            counts[a.family] = counts.get(a.family, 0) + 1
        industries = set(rt.industry_names())
        t = Table(title="red team families")
        for col in ("family", "kind", "attacks"):
            t.add_column(col)
        for name in rt.family_names():
            t.add_row(name, "industry" if name in industries else "mechanic",
                      str(counts.get(name, 0)))
        console.print(t)
        console.print(f"[bold]{len(rt.corpus())}[/bold] attacks across "
                      f"{len(rt.family_names())} families")
        return
    fams = [f.strip() for f in families.split(",") if f.strip()] if families else None
    try:
        attacks = rt.corpus(fams)
    except KeyError as e:
        console.print(f"[red]x[/red] {e}")
        sys.exit(1)
    cs = _resolve_checkset(checkset or "safety")
    from agentcheck import checks as check_lib
    from agentcheck.judges import get_judge
    from agentcheck.judges.base import Answer as _A
    cset = check_lib.get(cs)
    j = get_judge(judge)
    console.print(f"red-teaming [bold]{judge}[/bold] with {len(attacks)} attacks "
                  f"across {len(fams) if fams else len(rt.family_names())} families "
                  f"({cs})…")
    from agentcheck import screens
    rows = []
    for a in attacks:
        judgment = j.ask(a.trace(), list(cset.checks))
        answers = {x.question_id: x for x in judgment.answers}
        v = answers.get(cset.verdict)
        verdict = str(v.value) if v else None
        conf = float(v.confidence) if v else 0.0
        # production parity: deterministic screens floor before scoring
        probe = {"trace_verdict": verdict}
        screens.apply(probe, a.trace())
        verdict = probe["trace_verdict"]
        rows.append({"id": a.id, "family": a.family, "verdict": verdict,
                     "confidence": round(conf, 4), "note": a.note,
                     "screen": probe.get("screen"),
                     "evaded": verdict == "pass"})
    by_fam: dict[str, dict] = {}
    for r in rows:
        s = by_fam.setdefault(r["family"], {"n": 0, "evaded": 0})
        s["n"] += 1
        s["evaded"] += 1 if r["evaded"] else 0
    t = Table(title=f"redteam: {judge} / {cs}")
    for col in ("family", "attacks", "evaded", "asr"):
        t.add_column(col)
    for fam in sorted(by_fam):
        s = by_fam[fam]
        t.add_row(fam, str(s["n"]), str(s["evaded"]),
                  f"{s['evaded'] / s['n']:.0%}")
    console.print(t)
    evaded = [r for r in rows if r["evaded"]]
    total_asr = len(evaded) / len(rows) if rows else 0.0
    console.print(f"attack success rate: [bold]{total_asr:.1%}[/bold] "
                  f"({len(evaded)}/{len(rows)})")
    for r in evaded:
        console.print(f"  [red]evaded[/red] {r['id']} conf={r['confidence']}: {r['note']}")
    floored = [r for r in rows if (r.get("screen") or {}).get("floor")]
    if floored:
        console.print(f"[dim]{len(floored)} attacks floored to review by "
                      f"deterministic screens (not judge catches)[/dim]")
    if out:
        Path(out).write_text(json.dumps({
            "judge": judge, "checkset": cs, "asr": total_asr,
            "by_family": by_fam, "attacks": rows,
        }, indent=2) + "\n")
        console.print(f"[green]ok[/green] wrote [bold]{out}[/bold]")
    if total_asr > max_asr:
        console.print(f"[red]gate: fail[/red] ASR {total_asr:.1%} > {max_asr:.1%}")
        sys.exit(1)


@cli.command()
@click.argument("action", type=click.Choice(["list", "show", "split", "add", "validate"]),
                default="list")
@click.argument("name", required=False)
@click.option("--in", "in_path", default=None, help="traces file for 'add'")
@click.option("--labeler", default=None, help="judge that labels on 'add'")
@click.option("--checkset", default=None)
@click.option("--note", default="")
def dataset(action: str, name: str | None, in_path: str | None,
            labeler: str | None, checkset: str | None, note: str) -> None:
    """Manage labeled datasets: list, show, split, add."""
    from agentcheck.evals import datasets as ds
    if action == "list":
        rows = ds.inventory()
        if not rows:
            console.print(f"[dim]no datasets under {ds.root()}[/dim]")
        t = Table(title=f"datasets ({ds.root()})")
        for col in ("name", "versions", "current", "n", "dev", "test", "labelers"):
            t.add_column(col)
        for r in rows:
            t.add_row(r["name"], str(r["versions"]), r["current"], str(r["n"]),
                      str(r["dev"]), str(r["test"]), ", ".join(r["labelers"]))
        if rows:
            console.print(t)
        return
    if not name:
        console.print("[red]x[/red] need a dataset name")
        sys.exit(1)
    if action == "show":
        rows = ds.load(name)
        inv = [r for r in ds.inventory() if r["name"] == ds._slug(name)]
        console.print(f"[bold]{name}[/bold]: {len(rows)} items"
                      + (f", versions {inv[0]['versions']}" if inv else ""))
        t = Table()
        for col in ("split", "gold", "labeler", "request"):
            t.add_column(col)
        for r in rows[:25]:
            tr = r.get("trace") or r
            t.add_row(ds.split_of(r), str(r.get("label_verdict")),
                      str(r.get("labeler_model") or r.get("labeler") or "?"),
                      str(tr.get("request", ""))[:60])
        console.print(t)
        if len(rows) > 25:
            console.print(f"[dim]… {len(rows) - 25} more[/dim]")
        return
    if action == "split":
        rows = ds.load(name)
        dev = ds.apply_split(rows, "dev")
        test = ds.apply_split(rows, "test")
        console.print(f"[bold]{name}[/bold]: dev {len(dev)}, test {len(test)} "
                      f"(deterministic by trace hash)")
        return
    if action == "validate":
        rows = ds.load(name)
        problems = ds.validate(rows)
        if not problems:
            console.print(f"[green]ok[/green] [bold]{name}[/bold]: "
                          f"all {len(rows)} rows well-formed")
            return
        console.print(f"[red]x[/red] [bold]{name}[/bold]: {len(problems)} problem(s)")
        for p in problems[:20]:
            console.print(f"  {p}")
        sys.exit(2)
    # add
    if not in_path:
        console.print("[red]x[/red] 'add' needs --in <traces file>")
        sys.exit(1)
    raw = json.loads(Path(in_path).read_text())
    traces = raw["traces"] if isinstance(raw, dict) else raw
    traces = [t["trace"] if isinstance(t.get("trace"), dict) else t for t in traces]
    problems = ds.validate(traces, require_labels=False)
    if problems:
        console.print(f"[red]x[/red] {len(problems)} invalid row(s):")
        for p in problems[:10]:
            console.print(f"  {p}")
        sys.exit(2)
    if labeler:
        cs = _resolve_checkset(checkset)
        console.print(f"labeling {len(traces)} traces with [bold]{labeler}[/bold] ({cs})…")
        rows = labels_mod.label_dataset(traces, labeler, checkset=cs, verbose=False)
    else:
        rows = traces
    p = ds.save(name, rows, note=note)
    console.print(f"[green]ok[/green] wrote [bold]{p}[/bold] ({len(rows)} items)")


@cli.command()
@click.option("--key", default=None, help="agentcheck api key")
@click.option("--url", default="http://127.0.0.1:7373")
@click.option("--bucket", default="day", type=click.Choice(["hour", "day", "week"]))
@click.option("--days", default=30)
@_data_dir_opt
@click.option("--all-keys", is_flag=True, help="roll up every key, not just one")
@click.option("--drift", is_flag=True, help="compare the newer half against the older")
@click.option("--json", "as_json", is_flag=True)
def monitor(key: str | None, url: str, bucket: str, days: int,
            data_dir: str | None, all_keys: bool, drift: bool, as_json: bool) -> None:
    """Judgment volume, verdict mix and confidence over time."""
    from agentcheck import monitor as mon
    import httpx
    store = _store(None if not data_dir else Path(data_dir))
    user_key = None
    if not all_keys:
        k = key or os.environ.get("AGENTCHECK_KEY")
        if not k:
            try:
                k = httpx.get(f"{url}/v1/bootstrap", timeout=10).json().get("key")
            except Exception:
                k = None
        user_key = k
        if user_key and store.lookup_key(user_key) is None:
            console.print(f"[yellow]![/yellow] key not in this store; "
                          "use --all-keys for the whole instance")
            user_key = None
    if drift:
        d = mon.drift(store, user_key, bucket, days)
        if as_json:
            console.print(json.dumps(d, indent=2))
            return
        if not d["ok"]:
            console.print(f"[yellow]![/yellow] {d['reason']}")
            return
        t = Table(title="drift")
        for col in ("window", "n", "flagged", "reviewed", "mean conf"):
            t.add_column(col)
        for label, part in (("earlier", d["early"]), ("later", d["late"])):
            t.add_row(label, str(part["n"]), f"{part['flagged_rate']:.1%}",
                      f"{part['review_rate']:.1%}", f"{part['mean_confidence']}")
        console.print(t)
        for k2, v in d["change"].items():
            console.print(f"  change {k2}: {v:+.4f}")
        for n in d["notes"]:
            console.print(f"[yellow]![/yellow] {n}")
        return
    tl = mon.timeline(store, user_key, bucket, days)
    if as_json:
        console.print(json.dumps(tl, indent=2))
        return
    t = Table(title=f"judgments by {bucket} (last {days}d)")
    for col in ("bucket", "n", "flagged", "reviewed", "mean conf", "signed out"):
        t.add_column(col)
    for b in tl["buckets"]:
        t.add_row(b["bucket"], str(b["n"]), f"{b['flagged_rate']:.1%}",
                  f"{b['review_rate']:.1%}",
                  "-" if b["mean_confidence"] is None else str(b["mean_confidence"]),
                  str(b["signed_out"]))
    console.print(t)
    console.print(f"total {tl['total']} judgments")


@cli.command(name="rag")
@click.option("--traces", required=True, help="JSON/JSONL of RAG traces")
@_judge_opt
@click.option("--min-faithfulness", default=None, type=float)
@click.option("--max-hallucination", default=None, type=float)
@click.option("--out", default=None)
def rag_cmd(traces: str, judge: str, min_faithfulness: float | None,
            max_hallucination: float | None, out: str | None) -> None:
    """RAG metrics: faithfulness, relevance, completeness, context use."""
    from agentcheck import metrics as M
    from agentcheck.judges import get_judge
    rows = _read_traces(traces)
    if not rows:
        console.print("[red]x[/red] no traces found")
        sys.exit(2)
    j = get_judge(judge)
    per_trace = []
    for i, t in enumerate(rows):
        if not M.is_rag_trace(t):
            console.print(f"[yellow]![/yellow] row {i} is not a RAG trace "
                          f"(needs question/answer/contexts); skipped")
            continue
        per_trace.append(M.score_trace(j, t))
    if not per_trace:
        console.print("[red]x[/red] no RAG traces to score")
        sys.exit(2)
    summary = M.aggregate(per_trace)
    t = Table(title=f"rag metrics: {judge} ({len(per_trace)} traces)")
    for col in ("metric", "mean", "min", "max", "decisiveness", "model conf", "n"):
        t.add_column(col)
    for name, s in summary.items():
        conf = "n/a" if s["mean_confidence"] is None else f"{s['mean_confidence']:.2f}"
        t.add_row(name, f"{s['mean']:.3f}", f"{s['min']:.3f}", f"{s['max']:.3f}",
                  f"{s['mean_decisiveness']:.2f}", conf, str(s["n"]))
    console.print(t)
    console.print("[dim]'model conf' is n/a for Noul-derived metrics: System One does "
                  "not return a confidence field for Noul answers, only a "
                  "probability. 'decisiveness' is |p-0.5|*2, a different quantity.[/dim]")
    thresholds: dict[str, float] = {}
    if min_faithfulness is not None:
        thresholds["faithfulness"] = min_faithfulness
    if max_hallucination is not None:
        thresholds["hallucination"] = max_hallucination
    if out:
        Path(out).write_text(json.dumps({"judge": judge, "n": len(per_trace),
                                         "summary": summary}, indent=2) + "\n")
        console.print(f"[green]ok[/green] wrote [bold]{out}[/bold]")
    if thresholds:
        v = M.thresholds_pass(summary, thresholds, direction={"hallucination": "lower"})
        if not v["ok"]:
            console.print("[red]gate: fail[/red]")
            for f in v["failures"]:
                console.print(f"  {f['metric']}: {f.get('mean')} (want {f.get('want')})")
            sys.exit(1)
        console.print("[green]gate: pass[/green]")


@cli.command(name="ci-init")
def ci_init() -> None:
    """Write a starter GitHub Action scoring traces/ on every PR."""
    workflow = (
        "name: agentcheck\n"
        "on:\n"
        "  pull_request:\n"
        "    paths: ['traces/**.json', 'traces/**.jsonl']\n"
        "\njobs:\n"
        "  review:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.11'\n"
        "      - run: pip install agentcheck\n"
        "      - name: Score traces\n"
        "        env:\n"
        "          AGENTCHECK_KEY: ${{ secrets.AGENTCHECK_KEY }}\n"
        "          AGENTCHECK_URL: ${{ secrets.AGENTCHECK_URL }}\n"
        "        run: |\n"
        "          agentcheck ci --traces traces/ --fail-on fail >> $GITHUB_STEP_SUMMARY\n"
    )
    path = Path(".github/workflows/agentcheck.yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow)
    console.print(f"[green]ok[/green] wrote [bold]{path}[/bold]")
    console.print("Set [bold]AGENTCHECK_KEY[/bold] and [bold]AGENTCHECK_URL[/bold] as repo secrets.")


@cli.command()
@_data_dir_opt
@_judge_opt
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=7373)
@click.option("--demo", is_flag=True, default=False,
              help="Hosted demo: /v1/bootstrap hands out a capped demo key "
                   "from any origin (10 req/min, 300 questions/month).")
def serve(data_dir: str | None, judge: str, host: str, port: int,
          demo: bool) -> None:
    """Run the metered proxy locally."""
    import uvicorn
    from agentcheck.proxy import create_app
    store = _store(None if not data_dir else Path(data_dir))
    # Pass None (not False) when the flag is absent so create_app can fall
    # back to the AGENTCHECK_DEMO env var — containers have no flag to pass.
    app = create_app(store, default_judge=judge, demo_mode=demo or None)
    # Print the judge that was actually resolved, not the one requested: with
    # no API key the app falls back to the stub, and a banner claiming
    # otherwise sends people hunting for a bug that is a missing key.
    console.print(f"[bold]agentcheck[/bold] workspace http://{host}:{port}/  "
                  f"judge={app.state.judge}  available={judges_available()}")
    console.print("Paste a key from [bold]agentcheck key …[/bold] to open the log.")
    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command(name="auth")
def auth_cmd() -> None:
    """Identity status, and the redirect URI to register with the provider.

    The most common setup failure is a redirect URI that does not match, which
    the provider reports only in the browser. Printing the exact value here
    turns a confusing error page into a copy-and-paste.
    """
    from agentcheck import auth
    provider = auth.get_provider()
    base = (os.environ.get("AGENTCHECK_BASE_URL") or "").rstrip("/")
    t = Table(title=f"sign-in (provider: {provider.name})", show_header=False)
    t.add_row("configured", "yes" if provider.configured() else "no")
    t.add_row("redirect URI", f"{base}/v1/auth/callback" if base
              else "[red]set AGENTCHECK_BASE_URL[/red]")
    t.add_row("secret", "set" if auth.secret() else "[red]unset[/red]")
    console.print(t)
    if provider.configured():
        console.print("[green]sign-in armed[/green]")
        if not base:
            console.print("[yellow]![/yellow] AGENTCHECK_BASE_URL is unset: "
                          "behind a proxy the derived redirect URI will be "
                          "wrong and the provider will refuse the login.")
        console.print("[dim]Register the redirect URI above with the provider "
                      "before the first login.[/dim]")
    else:
        console.print("[yellow]![/yellow] sign-in is NOT armed. Set "
                      "AGENTCHECK_AUTH=workos plus WORKOS_CLIENT_ID, "
                      "WORKOS_API_KEY, AGENTCHECK_SECRET and "
                      "AGENTCHECK_BASE_URL. (Or AGENTCHECK_AUTH=oidc plus the "
                      "AGENTCHECK_OIDC_* three, for a self-hosted IdP.)")
    return


@cli.command(name="waitlist")
def waitlist_cmd() -> None:
    """Who wants hosted, before there is anything to sell.

    The pricing page has no checkout yet, so sign-ups are the only demand
    signal that exists. Addresses are other people's data; this reads them
    from the local store, for the operator.
    """
    store = _store(None)
    rows = store.waitlist()
    if not rows:
        console.print("[dim]nobody on the waitlist yet.[/dim]")
        console.print("The pricing page at /start collects them; the endpoint "
                      "is POST /v1/waitlist.")
        return
    by_plan: dict[str, int] = {}
    for r in rows:
        by_plan[r.get("plan") or "unsure"] = by_plan.get(r.get("plan")
                                                          or "unsure", 0) + 1
    t = Table(title=f"waitlist: {len(rows)} sign-ups")
    t.add_column("when")
    t.add_column("email")
    t.add_column("plan")
    for r in rows:
        t.add_row(
            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"])),
            r["email"], r.get("plan") or "-")
    console.print(t)
    console.print("by plan: " + ", ".join(f"{k} {v}" for k, v in
                                        sorted(by_plan.items())))


@cli.command(name="billing")
@click.option("--plan", default=None, help="show the provider id needed for a tier")
def billing_cmd(plan: str | None) -> None:
    """Plans, and whether this deployment can actually take money.

    Reports honestly: an unconfigured deployment says so rather than listing
    a checkout that would 503.
    """
    from agentcheck import billing
    provider = billing.get_provider()
    t = Table(title=f"plans (provider: {provider.name})")
    for col in ("plan", "allowance/mo", "qpm", "price", "provider plan id"):
        t.add_column(col)
    for p in billing.plans_public():
        if p.get("price") is None:
            pid, price = "custom", "custom"
        elif not p.get("price"):
            pid, price = "-", "free"
        else:
            try:
                pid = billing.plan_id_for(p["name"])
            except billing.BillingError:
                pid = "[red]not set[/red]"
            # The published price is the USD figure. The provider's own
            # currency is shown BESIDE it for the operator, never on a
            # customer surface: a page that advertises dollars and debits
            # rupees is a bait-and-switch, and checkout refuses that pairing.
            price = f"USD {p['price_usd']:,}"
            if p.get("currency") and p["currency"] != billing.DISPLAY_CURRENCY:
                price += f" [red]provider settles {p['currency']} — not sellable[/red]"
        t.add_row(
            p["name"], f"{p['allowance']:,} questions", f"{p['qpm']:,}/min",
            price, pid)
    console.print(t)
    if provider.configured():
        console.print("[green]billing armed[/green] "
                      f"(AGENTCHECK_BILLING={provider.name})")
    else:
        console.print("[yellow]![/yellow] billing is NOT armed. Set "
                      "AGENTCHECK_BILLING=razorpay plus RAZORPAY_KEY_ID, "
                      "RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET and one "
                      "RAZORPAY_PLAN_ID_<TIER> per paid tier.")
        console.print("[dim]credentials alone never arm it on purpose: they "
                      "may be live, and a shared account may hold plans for "
                      "other products.[/dim]")


if __name__ == "__main__":
    cli()
