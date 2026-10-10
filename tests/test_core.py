import pytest

from dockctl.actions import hotkey_script
from dockctl.config import Config, ConfigError
from dockctl.device import MODELS
from dockctl.render import Face, chunk_text, parse_color, render_face


@pytest.fixture
def cfg(tmp_path):
    return Config(tmp_path / "c.toml")


def test_set_merge_and_replace_action(cfg):
    cfg.set_key("main", 1, {"label": "A", "run": "echo hi"})
    cfg.set_key("main", 1, {"open": "https://x.y"})
    raw = cfg.page_raw("main")["1"]
    assert raw == {"label": "A", "open": "https://x.y"}  # new action replaces old one, look kept
    cfg.set_key("main", 1, {"label": None})
    assert "label" not in cfg.page_raw("main")["1"]


def test_roundtrip_keeps_comments(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# hello\nbrightness = 50\n\n[pages.main.1]\nlabel = "x" # inline\n')
    c = Config(p)
    c.set_key("main", 2, {"label": "y"})
    c.save()
    text = p.read_text()
    assert "# hello" in text and "# inline" in text and Config(p).key("main", 2).face.label == "y"


def test_empty_page_survives_save(tmp_path):
    c = Config(tmp_path / "c.toml")
    c.set_key("main", 1, {"label": "go", "page": "other"})
    c.ensure_page("other")
    c.save()
    assert Config(tmp_path / "c.toml", strict=True).pages() == ["main", "other"]


def test_validation(cfg):
    with pytest.raises(ConfigError):
        cfg.set_key("main", 1, {"run": "a", "open": "b"})
    with pytest.raises(ConfigError):
        cfg.set_key("main", 1, {"bogus": 1})
    with pytest.raises(ConfigError):
        cfg.set_key("main", 1, {"bg": "notacolour"})


def test_move_swaps(cfg):
    cfg.set_key("main", 1, {"label": "a"})
    cfg.set_key("main", 2, {"label": "b"})
    cfg.move_key("main", 1, 2)
    assert cfg.key("main", 1).face.label == "b" and cfg.key("main", 2).face.label == "a"


def test_rename_page_updates_links(cfg):
    cfg.set_key("main", 1, {"label": "go"})
    cfg.ensure_page("media")
    cfg.set_key("media", 1, {"page": "main"})
    cfg.set_top("start_page", "main")
    cfg.rename_page("main", "home")
    assert cfg.page_raw("media")["1"]["page"] == "home" and cfg.start_page == "home"


def test_style_defaults_apply(cfg):
    cfg.set_style({"bg": "#123456"})
    cfg.set_key("main", 1, {"label": "x"})
    cfg.set_key("main", 2, {"label": "y", "bg": "red"})
    assert cfg.key("main", 1).face.bg == "#123456"
    assert cfg.key("main", 2).face.bg == "red"


def test_hotkeys():
    assert hotkey_script("cmd+shift+4") == 'tell application "System Events" to keystroke "4" using {command down, shift down}'
    assert hotkey_script("f5").endswith("key code 96")
    with pytest.raises(ValueError):
        hotkey_script("hyper+x")


def test_colors():
    assert parse_color("#fff") == (255, 255, 255)
    assert parse_color("green") == (22, 163, 74)


def test_render_sizes():
    img = render_face(Face(label="Hello world", icon="🚀", bg="blue"), 85)
    assert img.size == (85, 85)



def test_chunk_text_keeps_words_whole():
    assert chunk_text("Deploy?", 5, 95) == ["Deploy?"]
    chunks = chunk_text("Is this question readable on the device?", 5, 95)
    assert 1 < len(chunks) <= 5 and " ".join(chunks) == "Is this question readable on the device?"
    long = " ".join(["word"] * 60)
    assert len(chunk_text(long, 5, 95)) == 5


def test_key_mapping_is_a_bijection():
    for m in MODELS.values():
        ids = [m.device_id(k) for k in range(1, m.total_keys + 1)]
        assert len(set(ids)) == len(ids)
        assert all(m.logical_key(m.device_id(k)) == k for k in range(1, m.total_keys + 1))


def test_global_keys_show_on_every_page_unless_overridden(cfg):
    from dockctl.config import GLOBAL

    cfg.set_key(GLOBAL, 18, {"poll": "date +%H:%M"})
    cfg.set_key("a", 1, {"label": "x"})
    cfg.set_key("b", 18, {"label": "own"})
    assert cfg.pages() == ["a", "b"]
    assert cfg.keys("a")[18].options["poll"] == "date +%H:%M" and cfg.is_global("a", 18)
    assert cfg.keys("b")[18].face.label == "own" and not cfg.is_global("b", 18)


def test_side_column_is_display_only(cfg):
    with pytest.raises(ConfigError, match="display-only"):
        cfg.set_key("main", 17, {"label": "x", "run": "true"})
    cfg.set_key("main", 17, {"label": "ok", "poll": "echo hi"})


def test_device_key_ids_match_hardware():
    from dockctl.device import DEFAULT_MODEL as m

    # verified on an AKP153E (0300:3010): ids run column by column from the top-right
    assert [m.device_id(k) for k in (1, 5, 6, 10, 11, 15, 16, 18)] == [13, 1, 14, 2, 15, 3, 16, 18]
    assert m.key_at(2, 3) == 8 and m.key_at(1, 6) == 16


def test_parse_input():
    from dockctl.device import DEFAULT_MODEL, Dock

    d = Dock.__new__(Dock)
    d.model = DEFAULT_MODEL
    press = bytes.fromhex("41434b00004f4b00000d01") + bytes(501)
    release = bytes.fromhex("41434b00004f4b00000d00") + bytes(501)
    ack = bytes.fromhex("41434b00004f4b0000ff01") + bytes(501)
    assert d.parse_input(press).key == 1 and d.parse_input(press).pressed
    assert not d.parse_input(release).pressed
    assert d.parse_input(ack) is None
