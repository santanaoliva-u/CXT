import os
import stat
import json
import sys
import types

import scheduler as S


def _result(*, color="green", text="Texto verde", angle_id="global", angles=None, ok=True, error=None):
    return {
        "ok": ok,
        "run_id": "run1",
        "error": error,
        "draft": {"text": text, "angle_id": angle_id} if text is not None else None,
        "stamp": {
            "color": color,
            "publishable": color == "green",
            "reasons": [] if color == "green" else ["motivo de prueba"],
        },
        "angles": angles or [{"id": "global"}, {"id": "mexico"}, {"id": "local"}],
    }


def _cfg(text="tema"):
    return {"topic": "editorial", "text": text, "editorial": {}}


def test_gen_editorial_publica_solo_verde(monkeypatch):
    calls = []

    def generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))
        return _result()

    monkeypatch.setitem(sys.modules, "editorial", types.SimpleNamespace(generate=generate))
    text = S.gen_editorial(_cfg())

    assert text == "Texto verde"
    assert S.EDITORIAL_META["stamp_color"] == "green"
    assert S.EDITORIAL_META["run_id"] == "run1"
    assert S.EDITORIAL_LAST_ERROR == ""
    assert calls and calls[0][0] == "generate"


def test_gen_editorial_reintenta_otro_angulo_y_publica_verde(monkeypatch):
    retries = []

    def generate(*args, **kwargs):
        return _result(color="amber", text="Texto ámbar")

    def draft_for_run(run_id, *, angle_id=None, **kwargs):
        retries.append((run_id, angle_id))
        return _result(color="green", text="Texto verde", angle_id=angle_id)

    monkeypatch.setitem(sys.modules, "editorial", types.SimpleNamespace(
        generate=generate, draft_for_run=draft_for_run))

    text = S.gen_editorial(_cfg())

    assert text == "Texto verde"
    assert retries[0] == ("run1", "mexico")
    assert S.EDITORIAL_META["attempt"] == 2


def test_gen_editorial_no_publica_ambar_ni_rojo(monkeypatch):
    def generate(*args, **kwargs):
        return _result(color="amber", text="Texto ámbar")

    def draft_for_run(run_id, *, angle_id=None, **kwargs):
        return _result(color="red", text="Texto rojo", angle_id=angle_id)

    monkeypatch.setitem(sys.modules, "editorial", types.SimpleNamespace(
        generate=generate, draft_for_run=draft_for_run))

    text = S.gen_editorial(_cfg())

    assert text == ""
    assert S.EDITORIAL_META["publishable"] is False
    assert "sello" in S.EDITORIAL_LAST_ERROR


def test_gen_editorial_sin_borrador_no_publica(monkeypatch):
    def generate(*args, **kwargs):
        return _result(color="red", text=None, ok=False, error="sin fuentes: no hay vista previa")

    monkeypatch.setitem(sys.modules, "editorial", types.SimpleNamespace(generate=generate))
    text = S.gen_editorial(_cfg())

    assert text == ""
    assert "sin fuentes" in S.EDITORIAL_LAST_ERROR


def test_apply_cmd_copia_repeat_y_editorial(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({
        "enabled": True,
        "interval_min": 60,
        "count": 1,
        "repeat": True,
        "topic": "editorial",
        "text": "tema",
        "editorial": {"max_length": 500},
    }), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))

    cfg = S.apply_cmd(S.defaults())

    assert cfg["enabled"] is True
    assert cfg["repeat"] is True
    assert cfg["topic"] == "editorial"
    assert cfg["editorial"] == {"max_length": 500}
    assert not cmd.exists()


def test_publish_retryable_solo_antes_del_clic():
    assert S.publish_retryable({"ok": False, "stage": "composer-not-found"})
    assert S.publish_retryable({"ok": False, "stage": "modal"})
    assert S.publish_retryable({"ok": False, "stage": "publicar"})
    assert S.publish_retryable({"ok": False, "error": "sin salida"})
    assert not S.publish_retryable({"ok": True, "stage": "modal"})
    assert not S.publish_retryable({"ok": False})
    assert not S.publish_retryable({"ok": False, "error": "no confirmo el cierre"})


def test_apply_cmd_sin_repeat_no_hereda_repeat(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({
        "enabled": True,
        "interval_min": 5,
        "count": 1,
        "topic": "fixed",
        "text": "uno",
    }), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))

    cfg = S.defaults()
    cfg["repeat"] = True
    cfg = S.apply_cmd(cfg)

    assert cfg["enabled"] is True
    assert cfg["repeat"] is False


def test_compose_text_enruta_editorial(monkeypatch):
    monkeypatch.setattr(S, "gen_editorial", lambda cfg: "desde editorial")
    assert S.compose_text({"topic": "editorial"}) == "desde editorial"


def test_apply_cmd_preview_no_muta_programacion(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({
        "action": "preview",
        "enabled": False,
        "interval_min": 5,
        "count": 9,
        "repeat": False,
        "topic": "fixed",
        "text": "vista original",
    }), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))
    monkeypatch.setattr(S, "compose_text", lambda cfg: "texto de vista")

    cfg = S.defaults()
    cfg.update({
        "enabled": True,
        "interval_min": 60,
        "count": 3,
        "remaining": 2,
        "repeat": True,
        "topic": "editorial",
        "text": "original",
        "started_at": 111,
        "next_at": 222,
    })
    before = {key: cfg[key] for key in (
        "enabled", "interval_min", "count", "remaining", "repeat",
        "topic", "text", "started_at", "next_at",
    )}

    out = S.apply_cmd(cfg)

    assert {key: out[key] for key in before} == before
    assert out["preview"]["ok"] is True
    assert out["preview"]["text"] == "texto de vista"
    assert out["preview"]["topic"] == "fixed"
    assert out["preview"]["source"] == {"topic": "fixed", "text": "vista original"}
    assert out["preview"]["meta"] == {}


def test_apply_cmd_preview_editorial_expone_sello_sin_mutar(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({
        "action": "preview",
        "topic": "editorial",
        "text": "tema",
    }), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))
    monkeypatch.setattr(S, "compose_text", lambda cfg: "")
    meta = {"run_id": "run2", "stamp_color": "amber", "publishable": False, "attempt": 2}
    monkeypatch.setattr(S, "EDITORIAL_META", meta)
    monkeypatch.setattr(S, "EDITORIAL_LAST_ERROR", "sello amber: falta fuente")

    cfg = S.defaults()
    cfg.update({
        "enabled": True,
        "remaining": 4,
        "next_at": 999,
        "topic": "editorial",
        "text": "tema",
    })

    out = S.apply_cmd(cfg)

    assert out["enabled"] is True
    assert out["remaining"] == 4
    assert out["next_at"] == 999
    assert out["topic"] == "editorial"
    assert out["text"] == "tema"
    assert out["preview"]["ok"] is False
    assert out["preview"]["meta"] == meta
    assert out["preview"]["error"] == "sello amber: falta fuente"



def test_apply_cmd_publish_now_preserva_programacion(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({
        "action": "publish_now",
        "text": "texto manual",
        "topic": "fixed",
        "meta": {"run_id": "r1", "stamp_color": "green"},
    }), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))

    cfg = S.defaults()
    cfg.update({
        "enabled": True,
        "interval_min": 60,
        "count": 3,
        "remaining": 2,
        "repeat": True,
        "topic": "editorial",
        "text": "original",
        "started_at": 111,
        "next_at": 222,
        "preview": {"ok": True, "text": "vista", "meta": {"run_id": "pv1"}},
    })
    before = {key: cfg[key] for key in (
        "enabled", "interval_min", "count", "remaining", "repeat",
        "topic", "text", "started_at", "next_at", "preview",
    )}

    out = S.apply_cmd(cfg)

    assert {key: out[key] for key in before} == before
    assert out["pending"]["text"] == "texto manual"
    assert out["pending"]["topic"] == "fixed"
    assert out["pending"]["meta"] == {"run_id": "r1", "stamp_color": "green"}
    assert not cmd.exists()


def test_publish_pending_publica_sin_tocar_programacion(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "CFG", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(S, "publish", lambda text: {"ok": True})
    monkeypatch.setattr(S, "log", lambda msg: None)

    cfg = S.defaults()
    cfg.update({
        "enabled": True,
        "interval_min": 60,
        "count": 3,
        "remaining": 2,
        "repeat": True,
        "topic": "editorial",
        "text": "original",
        "next_at": 9999999999999,
        "pending": {"text": "texto manual", "topic": "fixed",
                    "meta": {"run_id": "r1", "stamp_color": "green"}},
    })

    out, res = S.publish_pending(cfg)

    assert res == {"ok": True}
    assert "pending" not in out
    assert out["last"]["ok"] is True
    assert out["last"]["text"] == "texto manual"
    assert out["last"]["run_id"] == "r1"
    assert out["last"]["stamp_color"] == "green"
    assert out["enabled"] is True
    assert out["repeat"] is True
    assert out["remaining"] == 2
    assert out["next_at"] == 9999999999999
    assert out["topic"] == "editorial"
    assert out["text"] == "original"


def test_publish_pending_fallo_no_apaga_programacion(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "CFG", str(tmp_path / "schedule.json"))
    monkeypatch.setattr(S, "publish", lambda text: {"ok": False, "stage": "modal"})
    monkeypatch.setattr(S, "log", lambda msg: None)
    monkeypatch.setattr(S, "EDITORIAL_META", {})

    cfg = S.defaults()
    cfg.update({
        "enabled": True,
        "interval_min": 60,
        "count": 3,
        "remaining": 2,
        "repeat": True,
        "topic": "editorial",
        "text": "original",
        "next_at": 9999999999999,
        "pending": {"text": "texto manual", "topic": "editorial", "meta": {}},
    })

    out, res = S.publish_pending(cfg)

    assert res == {"ok": False, "stage": "modal"}
    assert "pending" not in out
    assert out["last"]["ok"] is False
    assert out["last"]["error"] == "modal"
    assert out["last"]["run_id"] == ""
    assert out["enabled"] is True
    assert out["remaining"] == 2
    assert out["next_at"] == 9999999999999


def test_publish_usa_temporal_privado_y_no_sigue_symlink(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("contenido original intacto", encoding="utf-8")
    ataque = tmp_path / "cxt_auto_post.txt"
    ataque.symlink_to(victim)

    vistos = {}

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["python3", "-u", S.PUB]
        path = argv[3]
        vistos["path"] = path
        vistos["parent"] = os.path.dirname(path)
        vistos["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        with open(path, encoding="utf-8") as handle:
            vistos["texto"] = handle.read()
        return types.SimpleNamespace(stdout='{"ok": true}\n', stderr="", returncode=0)

    monkeypatch.setattr(S, "DIR", str(tmp_path))
    monkeypatch.setattr(S, "resolve_tab", lambda: "1")
    monkeypatch.setattr(S, "log", lambda msg: None)
    monkeypatch.setattr(S.subprocess, "run", fake_run)

    res = S.publish("texto de prueba")

    assert res == {"ok": True}
    assert vistos["parent"] == str(tmp_path)
    assert os.path.basename(vistos["path"]).startswith("cxt_auto_post_")
    assert vistos["mode"] == 0o600
    assert vistos["texto"] == "texto de prueba"
    assert not os.path.exists(vistos["path"])
    assert list(tmp_path.glob("cxt_auto_post_*.txt")) == []
    assert ataque.is_symlink()
    assert ataque.resolve() == victim
    assert victim.read_text(encoding="utf-8") == "contenido original intacto"


def test_write_pid_es_atomico_y_limpio(tmp_path, monkeypatch):
    pid_file = tmp_path / "scheduler.pid"
    monkeypatch.setattr(S, "PID", str(pid_file))
    S._write_pid()
    assert pid_file.read_text(encoding="utf-8") == str(os.getpid())
    assert [p.name for p in tmp_path.iterdir()] == ["scheduler.pid"]


def test_write_pid_fallido_preserva_pid_viejo(tmp_path, monkeypatch):
    pid_file = tmp_path / "scheduler.pid"
    pid_file.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(S, "PID", str(pid_file))

    def boom(a, b):
        raise OSError("replace roto")

    monkeypatch.setattr(S.os, "replace", boom)
    try:
        S._write_pid()
    except OSError as exc:
        assert "replace roto" in str(exc)
    else:
        raise AssertionError("deberia propagar el OSError")
    assert pid_file.read_text(encoding="utf-8") == "12345"
    assert [p.name for p in tmp_path.iterdir()] == ["scheduler.pid"]


def test_consume_cmd_ausente_no_falla(tmp_path):
    assert S._consume_cmd(str(tmp_path / "no-existe.json")) is True


def test_cmd_no_consumible_no_publica_ni_repite(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({"action": "publish_now", "text": "texto aprobado"}), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))
    logs = []
    monkeypatch.setattr(S, "log", lambda msg: logs.append(str(msg)))

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(S.os, "remove", boom)
    monkeypatch.setattr(S.os, "replace", boom)

    cfg = S.apply_cmd(S.defaults())

    assert "pending" not in cfg
    assert cmd.exists()
    assert any("comando ignorado" in m for m in logs)


def test_cmd_se_aparta_si_no_se_puede_borrar(tmp_path, monkeypatch):
    cmd = tmp_path / "schedule.cmd.json"
    cmd.write_text(json.dumps({"action": "publish_now", "text": "texto aprobado"}), encoding="utf-8")
    monkeypatch.setattr(S, "CMD", str(cmd))
    logs = []
    monkeypatch.setattr(S, "log", lambda msg: logs.append(str(msg)))

    def boom(*a, **k):
        raise OSError("operacion no permitida")

    monkeypatch.setattr(S.os, "remove", boom)

    cfg = S.apply_cmd(S.defaults())

    assert cfg["pending"]["text"] == "texto aprobado"
    assert not cmd.exists()
    assert list(tmp_path.glob("schedule.cmd.json.failed-*"))
    assert any("cmd apartado" in m for m in logs)


def test_publish_pending_persiste_antes_de_publicar(tmp_path, monkeypatch):
    cfg_path = tmp_path / "schedule.json"
    monkeypatch.setattr(S, "CFG", str(cfg_path))
    monkeypatch.setattr(S, "log", lambda msg: None)
    seen = {}

    def fake_publish(text):
        seen["en_disco"] = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {"ok": True}

    monkeypatch.setattr(S, "publish", fake_publish)

    cfg = S.defaults()
    cfg["pending"] = {"text": "texto manual", "topic": "fixed", "meta": {}}

    out, res = S.publish_pending(cfg)

    assert res == {"ok": True}
    assert "pending" not in seen["en_disco"]
    assert seen["en_disco"]["last"] is None
    assert "pending" not in out


def test_publish_pending_no_publica_si_no_puede_persistir(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "CFG", str(tmp_path / "schedule.json"))
    logs = []
    monkeypatch.setattr(S, "log", lambda msg: logs.append(str(msg)))
    llamadas = []
    monkeypatch.setattr(S, "publish", lambda text: llamadas.append(text) or {"ok": True})

    def no_write(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(S, "write_json", no_write)

    cfg = S.defaults()
    cfg["pending"] = {"text": "texto manual", "topic": "fixed", "meta": {}}

    out, res = S.publish_pending(cfg)

    assert res is None
    assert llamadas == []
    assert out["last"] is None
    assert any("no se pudo persistir" in m for m in logs)
