import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.services.widget_js import generate_script_tag, render_widget_js

HARNESS = Path(__file__).with_name("harness_foreign_origin.js")
API_HOST = "api.flyrank.example"


class TestRenderWidgetJs:
    def test_js_generation_returns_string(self):
        js = render_widget_js("test-id", {"brand_color": "#000000"}, 1)
        assert isinstance(js, str)
        assert len(js) > 100

    def test_js_contains_widget_id(self):
        js = render_widget_js("widget-123", {}, 1)
        assert "widget-123" in js

    def test_js_references_config_at_runtime(self):
        js = render_widget_js("widget-1", {}, 1)
        assert "config.brand_color" in js
        assert "config.button_text" in js
        assert "config.honeypot_field" in js

    def test_uses_self_invoking_function(self):
        js = render_widget_js("test-id", {}, 1)
        assert "(function()" in js
        assert "})()" in js or "})();" in js

    def test_client_side_config_url_constructed_at_runtime(self):
        js = render_widget_js("widget-1", {}, 1)
        assert "configUrl" in js
        assert "/public/widget/" in js


class TestCrossOriginRender:
    def test_bundle_derives_base_from_current_script(self):
        js = render_widget_js("widget-1", {}, 1)
        assert "document.currentScript" in js
        assert "data-api-base" in js
        assert "window.location.origin" in js

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="node runtime not available for bundle execution harness",
    )
    def test_config_and_submit_resolve_to_script_origin(self, tmp_path):
        bundle = tmp_path / "widget.js"
        bundle.write_text(render_widget_js("abc", {}, 1), encoding="utf-8")

        result = subprocess.run(
            [shutil.which("node"), str(HARNESS), str(bundle)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        urls = json.loads(result.stdout)
        for key in ("config", "submit"):
            host = urlparse(urls[key]).hostname
            assert host == API_HOST, (
                f"{key} URL resolved to {host}, expected {API_HOST} "
                f"(script origin, not the embedding page origin)"
            )

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="node runtime not available for bundle execution harness",
    )
    def test_config_and_submit_honor_data_api_base_override(self, tmp_path):
        bundle = tmp_path / "widget.js"
        bundle.write_text(render_widget_js("abc", {}, 1), encoding="utf-8")

        env = dict(os.environ)
        env["HARNESS_SCRIPT_SRC"] = (
            "https://api.flyrank.example/public/widget/abc/widget.js?v=3"
        )
        env["HARNESS_API_BASE_OVERRIDE"] = "https://override.example"
        result = subprocess.run(
            [shutil.which("node"), str(HARNESS), str(bundle)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        urls = json.loads(result.stdout)
        for key in ("config", "submit"):
            host = urlparse(urls[key]).hostname
            assert host == "override.example", (
                f"{key} URL resolved to {host}, expected override.example "
                f"(data-api-base override takes precedence over script src)"
            )

    @pytest.mark.skipif(
        shutil.which("node") is None,
        reason="node runtime not available for bundle execution harness",
    )
    def test_config_and_submit_fall_back_to_page_origin_without_current_script(
        self, tmp_path
    ):
        bundle = tmp_path / "widget.js"
        bundle.write_text(render_widget_js("abc", {}, 1), encoding="utf-8")

        env = dict(os.environ)
        env["HARNESS_NO_CURRENT_SCRIPT"] = "1"
        result = subprocess.run(
            [shutil.which("node"), str(HARNESS), str(bundle)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        urls = json.loads(result.stdout)
        for key in ("config", "submit"):
            host = urlparse(urls[key]).hostname
            assert host == "customer-site.example", (
                f"{key} URL resolved to {host}, expected customer-site.example "
                f"(page-origin fallback only when currentScript is absent, i.e. "
                f"dynamically-injected scripts outside the documented pattern)"
            )


class TestGenerateScriptTag:
    def test_basic_script_tag(self):
        tag = generate_script_tag("abc-123", 1)
        assert 'src="http://localhost:8000/public/widget/abc-123/widget.js?v=1"' in tag
        assert 'data-widget-id="abc-123"' in tag
        assert "defer" in tag

    def test_script_tag_default_base_is_absolute(self):
        tag = generate_script_tag("wid", 1)
        assert tag.startswith('<script src="http://')

    def test_versioned_url_changes_with_version(self):
        tag_v1 = generate_script_tag("wid", 1)
        tag_v2 = generate_script_tag("wid", 2)
        assert "v=1" in tag_v1
        assert "v=2" in tag_v2
        assert tag_v1 != tag_v2

    def test_custom_base_url(self):
        tag = generate_script_tag("wid", 3, base_url="https://api.example.com")
        assert "https://api.example.com/public/widget/wid/widget.js?v=3" in tag

    def test_script_tag_format(self):
        tag = generate_script_tag("wid-99", 5)
        assert tag.startswith("<script ")
        assert tag.endswith("></script>")
