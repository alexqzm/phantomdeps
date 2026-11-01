import unittest

from phantomdeps.extract import extract
from phantomdeps.registries import crates_path, norm_pypi, norm_url
from phantomdeps.stats import cluster_bootstrap, wilson


def names(r, eco=None):
    return [(d["owner"], d["name"], d["kind"]) for d in r["decls"] if eco is None or d["eco"] == eco]


class Extraction(unittest.TestCase):
    def test_platformio(self):
        t = """```ini
[env:esp32]
platform = espressif32
lib_deps =
    - adafruit-bme280
    knolleary/PubSubClient @ ^2.8
    bblanchon/ArduinoJson@^6.21.3, Wire
    https://github.com/me/lib.git#v1
    166
monitor_speed = 115200
```"""
        r = extract(t, "platformio")
        self.assertEqual(names(r, "platformio"), [
            (None, "adafruit-bme280", "registry"), ("knolleary", "PubSubClient", "registry"),
            ("bblanchon", "ArduinoJson", "registry"), (None, "Wire", "registry"),
            (None, "https://github.com/me/lib.git#v1", "url"), (None, "166", "legacy_id")])

    def test_arduino_cli_quoted_and_unquoted(self):
        # unquoted words are separate libraries for arduino-cli, even when together they name one
        t = 'arduino-cli lib install "Adafruit BME280 Library" ArduinoJson@6.21.0\n' \
            'arduino-cli lib install Adafruit SSD1306'
        r = extract(t, "arduino", arduino_names={"adafruit ssd1306"})
        self.assertEqual([d["name"] for d in r["decls"]],
                         ["Adafruit BME280 Library", "ArduinoJson", "Adafruit", "SSD1306"])
        self.assertEqual([d["flags"] for d in r["decls"]][-2:], [["unquoted_multi"], ["unquoted_multi"]])

    def test_prose_is_ignored(self):
        r = extract("Install the **ModbusMaster** library from the Library Manager.", "arduino")
        self.assertEqual(r["decls"], [])

    def test_espidf_namespaces(self):
        t = """main/idf_component.yml:
```yaml
dependencies:
  idf: ">=5.0"
  espressif/led_strip: "^2.5"
  bme280: "*"
  mycomp:
    git: https://github.com/x/y.git
```"""
        r = extract(t, "espidf")
        self.assertEqual(names(r, "espidf"), [(None, "idf", "special"), ("espressif", "led_strip", "registry"),
                                              ("espressif", "bme280", "registry"), (None, "mycomp", "url")])

    def test_ros_local_and_apt(self):
        t = """```xml
<package format="3"><name>my_pkg</name>
<depend>rclcpp</depend><exec_depend>mavros_msgs</exec_depend><buildtool_depend>ament_cmake</buildtool_depend>
</package>
```
sudo apt install ros-humble-mavros-extras"""
        r = extract(t, "ros2")
        self.assertEqual(r["local"], ["my_pkg"])
        self.assertEqual(sorted(d["name"] for d in r["decls"]), ["ament_cmake", "mavros_extras", "mavros_msgs", "rclcpp"])

    def test_cargo_rename_path_and_bad_toml(self):
        t = """```toml
[package]
name = "fw"
[dependencies]
embedded-hal = "1.0"
hal = { package = "stm32f4xx-hal", version = "0.21", features = ["stm32f411"] }
mylocal = { path = "../x" }
```
```toml
[dependencies]
cortex-m-rt = "0.7"
bad = { version = "1"
```"""
        r = extract(t, "crates")
        self.assertIn(("crates", "stm32f4xx-hal"), [(d["eco"], d["name"]) for d in r["decls"]])
        self.assertIn(("crates", "cortex-m-rt"), [(d["eco"], d["name"]) for d in r["decls"]])
        self.assertIn("path", [d["kind"] for d in r["decls"] if d["name"] == "mylocal"])

    def test_pypi_and_npm(self):
        t = """pip install pymodbus==3.6 "influxdb-client[ciso]" -r requirements.txt --break-system-packages
requirements.txt:
```
pycomm3>=1.2
# comment
python-snap7
```
npm install node-red-contrib-s7 @flowfuse/node-red-dashboard@1.2 --save"""
        r = extract(t, "pypi")
        self.assertEqual([d["name"] for d in r["decls"] if d["eco"] == "pypi"],
                         ["pymodbus", "influxdb-client", "pycomm3", "python-snap7"])
        self.assertEqual([d["name"] for d in r["decls"] if d["eco"] == "npm"],
                         ["node-red-contrib-s7", "@flowfuse/node-red-dashboard"])

    def test_micropython(self):
        t = 'import mip\nmip.install("umqtt.simple")\nmip.install("github:org/repo")\n$ mpremote mip install aioble'
        r = extract(t, "micropython")
        self.assertEqual([(d["name"], d["kind"]) for d in r["decls"]],
                         [("umqtt.simple", "registry"), ("github:org/repo", "url"), ("aioble", "registry")])


class CommandContext(unittest.TestCase):
    def test_commands_inside_prose_are_skipped(self):
        t = "Sure: 1) arduino-cli lib install espressif/esp32-sensor@latest (for MPU6050), 2) Wire for I2C."
        self.assertEqual(extract(t, "arduino")["decls"], [])
        t = "   - arduino-cli lib install ds18x library for Arduino UNO  (https://github.com/x/y)"
        self.assertEqual(extract(t, "arduino")["decls"], [])
        t = "You can install it with pip install paho-mqtt and then run the script."
        self.assertEqual(extract(t, "pypi")["decls"], [])

    def test_code_context_and_prose_tail(self):
        t = 'Run `arduino-cli lib install "Adafruit BME280 Library"` and then upload.'
        self.assertEqual([d["name"] for d in extract(t, "arduino")["decls"]], ["Adafruit BME280 Library"])
        t = "```\npip install paho-mqtt for the broker\n```"
        self.assertEqual([d["name"] for d in extract(t, "pypi")["decls"]], ["paho-mqtt"])
        t = "1. npm install node-red-contrib-modbus node-red-dashboard"
        self.assertEqual([d["name"] for d in extract(t, "npm")["decls"]],
                         ["node-red-contrib-modbus", "node-red-dashboard"])


class LenientManifests(unittest.TestCase):
    def test_espidf_malformed_manifest_is_flagged(self):
        t = "```yaml\nidf_component:\n  name: x\n  dependencies:\n    - esp32_i2c_driver\n    - espressif/led_strip: '^2.0'\n```"
        r = extract(t, "espidf")
        self.assertEqual([(d["owner"], d["name"]) for d in r["decls"]],
                         [("espressif", "esp32_i2c_driver"), ("espressif", "led_strip")])
        self.assertTrue(all("malformed_manifest" in d["flags"] for d in r["decls"]))

    def test_espidf_list_of_name_fields(self):
        t = ("```yaml\nidf_component.yml:\n  dependencies:\n    - name: esp-mqtt\n      version: ^1.4.0\n"
             "    - name: ssd1306\n      path: components/ssd1306\n```")
        r = extract(t, "espidf")
        self.assertEqual(names(r, "espidf"), [("espressif", "esp-mqtt", "registry"), (None, "ssd1306", "path")])
        self.assertTrue(all("malformed_manifest" in d["flags"] for d in r["decls"]))

    def test_mislabelled_requirements_fence(self):
        ok = "```python\n# requirements.txt\npaho-mqtt==1.5.0\nsocketcan\n```"
        self.assertEqual([d["name"] for d in extract(ok, "pypi")["decls"]], ["paho-mqtt", "socketcan"])
        code = "```python\n# requirements.txt\nimport time\nx = 1\n```"
        self.assertEqual(extract(code, "pypi")["decls"], [])
        # introduced as the requirements file by the sentence before the fence
        intro = "Here is your `requirements.txt` file:\n```python3\npika==1.18.0\nRPi.GPIO==0.7.1  # GPIO\n```"
        self.assertEqual([d["name"] for d in extract(intro, "pypi")["decls"]], ["pika", "RPi.GPIO"])
        prog = "Your `requirements.txt`:\n```python\nimport socket\nsock = socket.socket()\n```"
        self.assertEqual(extract(prog, "pypi")["decls"], [])


class Helpers(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm_pypi("Python_Snap7"), "python-snap7")
        self.assertEqual(norm_url("https://github.com/RobTillaart/LTC2944.git"), "github.com/robtillaart/ltc2944")
        self.assertEqual(crates_path("embedded-hal"), "em/be/embedded-hal")
        self.assertEqual(crates_path("abc"), "3/a/abc")

    def test_wilson(self):
        p, lo, hi = wilson(10, 100)
        self.assertAlmostEqual(lo, 0.0552, places=3)
        self.assertAlmostEqual(hi, 0.1744, places=3)

    def test_kappa(self):
        from phantomdeps.validation import kappa
        self.assertAlmostEqual(kappa([True, True, False, False], [True, False, False, False]), 0.5)
        self.assertAlmostEqual(kappa([True, False], [True, False]), 1.0)

    def test_bootstrap_degenerate(self):
        p, lo, hi = cluster_bootstrap([(1, 2)] * 50)
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(lo, 0.5)
        self.assertAlmostEqual(hi, 0.5)


class Runner(unittest.TestCase):
    def test_reasoning_is_not_analysed(self):
        t = "<think>maybe pip install wrongpkg</think>\n```bash\npip install paho-mqtt\n```"
        r = extract(t, "pypi")
        self.assertEqual([d["name"] for d in r["decls"]], ["paho-mqtt"])
        self.assertTrue(r["reasoning_stripped"])
        self.assertEqual(extract("<think>pip install x", "pypi")["decls"], [])

    def test_model_params_and_families(self):
        from phantomdeps.llm import _families, _model_params
        cfg = {"families": ["F2"], "params": {"top_p": 0.95, "max_tokens": {"F1": 1536, "F2": 512},
                                              "repeat_penalty": 1.1},
               "models": [{"id": "a"}, {"id": "b", "families": ["F1", "F2"],
                                        "params": {"max_tokens": {"F1": 4096, "F2": 3072}}}]}
        self.assertEqual(_families(cfg), ("F2", "F1"))
        self.assertEqual(_model_params(cfg, cfg["models"][0], "F1"), (1536, 0.95, {"repeat_penalty": 1.1}))
        self.assertEqual(_model_params(cfg, cfg["models"][1], "F2"), (3072, 0.95, {"repeat_penalty": 1.1}))



class NamesWithoutLettersOrDigits(unittest.TestCase):
    def test_continuation_and_empty_list(self):
        from phantomdeps.extract import extract
        text = ("```ini\n[env:esp32]\nlib_deps =\n    bblanchon/ArduinoJson @ ^6.21 \\\n    \\\n```\n"
                "```properties\nname=Demo\ndepends=[]\n```")
        names = sorted(d["name"] for d in extract(text, "platformio")["decls"])
        self.assertEqual(names, ["ArduinoJson"])


class RequirementListsShareOneRule(unittest.TestCase):
    """extract.requirements_block decides for the extractor and for the parser check alike."""

    def test_config_after_the_word_requirements_is_not_a_requirement_list(self):
        from phantomdeps.extract import extract
        from phantomdeps.validity import assess
        text = ("Here is a platformio.ini that meets your requirements:\n```ini\n[env:esp32]\nplatform = espressif32\n"
                "board = esp32dev\nframework = arduino\n```")
        self.assertEqual([d for d in extract(text, "platformio")["decls"] if d["eco"] == "pypi"], [])
        self.assertEqual(assess(text, "platformio")["n_files"], 0)
        text2 = "Settings that meet your requirements:\n```\nhost = 0.0.0.0\nport = 8080\n```"
        self.assertEqual(extract(text2, "pypi")["decls"], [])

    def test_unlabelled_list_after_the_word_requirements(self):
        from phantomdeps.extract import extract
        from phantomdeps.validity import assess
        text = "Install the requirements:\n```\nflask>=2.0\npaho-mqtt==1.6.1\n```"
        self.assertEqual(sorted(d["name"] for d in extract(text, "pypi")["decls"]), ["flask", "paho-mqtt"])
        self.assertEqual(assess(text, "pypi")["files"][0]["kind"], "requirements.txt")


class ManifestsAsTheirToolsReadThem(unittest.TestCase):
    """Patterns of the study's answers where the extractor and the official readers disagreed."""

    def _agree(self, text, eco):
        from phantomdeps.validity import assess, extractor_names
        r = extract(text, eco)
        off = set()
        for f in assess(text, eco)["files"]:
            self.assertTrue(f["valid"], f)
            off |= set(f["names"])
        self.assertEqual(extractor_names(r["decls"]).get(f["eco"], set()), off)
        return r

    def test_platformio_comments_blank_lines_and_operators(self):
        t = """```ini
[env:esp32dev]
platform = espressif32
lib_deps =
    # PubSubClient library for MQTT communication (optional, if needed)
    ; Async Web Server, with WebSocket
    knolleary/PubSubClient@^2.8
    me-no-dev/AsyncTCP@^1.2.3  ; needs ESPAsyncWebServer, too

    ; sensors
    adafruit/Adafruit BME280 Library@^2.2.2
    Adafruit_BME280=*
    modbus-esp32 = ^1.0.0
    https://github.com/me/lib.git#v1
upload_speed = 921600
```"""
        r = self._agree(t, "platformio")
        self.assertEqual(names(r, "platformio"), [
            ("knolleary", "PubSubClient", "registry"), ("me-no-dev", "AsyncTCP", "registry"),
            ("adafruit", "Adafruit BME280 Library", "registry"), (None, "Adafruit_BME280", "registry"),
            (None, "modbus-esp32", "registry"), (None, "https://github.com/me/lib.git#v1", "url")])

    def test_platformio_value_ends_at_blank_line_before_a_key(self):
        t = "```ini\n[env:a]\nlib_deps =\n    Wire\n\nupload_speed = 1\n    stray\n```"
        self.assertEqual(names(extract(t, "platformio"), "platformio"), [(None, "Wire", "registry")])

    def test_package_xml_comments_and_export(self):
        t = """```xml
<package format="3">
  <name>demo</name>
  <version>0.1.0</version>
  <description>demo</description>
  <maintainer email="m@example.org">m</maintainer>
  <license>MIT</license>
  <depend>rclcpp</depend>
  <!-- <depend>rclcpp_components</depend> -->
  <!-- If you build messages
  <build_depend>message_generation</build_depend> -->
  <exec_depend>sensor_msgs</exec_depend>
  <export>
    <build_type>ament_cmake</build_type>
    <exec_depend>python3-yaml</exec_depend>
  </export>
</package>
```"""
        r = self._agree(t, "ros2")
        self.assertEqual(sorted(d["name"] for d in r["decls"]), ["rclcpp", "sensor_msgs"])
        t2 = t.replace("<export>", "<export/>\n  <test_depend>gtest</test_depend>\n  <export>")
        self.assertEqual(sorted(d["name"] for d in extract(t2, "ros2")["decls"]), ["gtest", "rclcpp", "sensor_msgs"])

    def test_requirement_lists_in_markdown_fences_and_pypi_tasks(self):
        t = "**requirements.txt**\n```markdown\n# Required libraries\nFlask==2.0.2\npandas==1.4.2\n```"
        r = self._agree(t, "pypi")
        self.assertEqual(sorted(d["name"] for d in r["decls"]), ["Flask", "pandas"])
        # no word before the fence, but the task asks for requirements.txt
        self.assertEqual(sorted(d["name"] for d in extract("```txt\nFlask\nplotly\n```", "pypi")["decls"]),
                         ["Flask", "plotly"])
        self.assertEqual(extract("```txt\nFlask\nplotly\n```", "ros2")["decls"], [])
        # a project tree is no requirement list, even in a PyPI task
        self.assertEqual(extract("```markdown\nmain.py\nrequirements.txt\nREADME.md\n```", "pypi")["decls"], [])

    def test_unfenced_requirements_after_the_file_name(self):
        t = "The packages:\n\nrequirements.txt:\n\ncan-tools==0.1.0\npython-can==1.10.1\n\nThen run main.py."
        r = extract(t, "pypi")
        self.assertEqual([(d["name"], d["via"]) for d in r["decls"]],
                         [("can-tools", "unfenced requirements.txt"), ("python-can", "unfenced requirements.txt")])
        self.assertEqual(extract("project/\nrequirements.txt\nmain.py\n", "pypi")["decls"], [])

    def test_commented_commands_and_micropython_cli(self):
        t = ("```python\n# mip.install('micropython-onewire')\nmip.install('ssd1306')\n"
             "import utime  # pip install micropython-utime\n```\n"
             "```bash\n# pip install old-thing\npip install new-thing  # comment\n"
             "micropython -m upip install adafruit-bme280\nmicropython -m mip install -p lib umqtt.simple\n```\n"
             "```js\n// npm install nope\n```")
        got = sorted((d["eco"], d["name"], d["via"]) for d in extract(t, "micropython")["decls"])
        self.assertEqual(got, [("micropython", "ssd1306", "mip.install"),
                               ("micropython", "umqtt.simple", "micropython -m mip"),
                               ("pypi", "adafruit-bme280", "micropython -m upip"),
                               ("pypi", "new-thing", "pip install")])

    def test_names_past_the_registry_limit_are_no_declarations_on_either_side(self):
        long = "esp32-" + "verify-" * 40
        t = f"```ini\n[env:a]\nlib_deps =\n    Wire\n    {long}\n```"
        r = self._agree(t, "platformio")
        self.assertEqual(names(r, "platformio"), [(None, "Wire", "registry")])


class SecondPreScreenFixes(unittest.TestCase):
    """Gaps that the second model pre-screen of the study sample pointed to (PROTOCOL.md, change
    log 2026-10-28)."""

    def test_capitalised_word_is_a_name_not_prose(self):
        # "can" ends a command as prose; the library CAN, in capitals, does not
        t = "```bash\narduino-cli lib install CAN\narduino-cli lib install MCP2515 CAN Bus\n```\n" \
            "`arduino-cli lib install SPI can be skipped`"
        self.assertEqual([(d["name"], d["flags"]) for d in extract(t, "arduino")["decls"]],
                         [("CAN", []), ("MCP2515", ["unquoted_multi"]), ("Bus", ["unquoted_multi"]), ("SPI", [])])

    def test_hash_inside_an_argument_is_part_of_it(self):
        # the shell starts a comment only at the start of a word; arduino-cli splits a version off
        # at '@' only, so it looks up the whole token (and finds nothing)
        t = '```bash\narduino-cli lib install "SD#2.2.2" Servo@1.2.1 # comment\n```'
        self.assertEqual([(d["name"], d["version"]) for d in extract(t, "arduino")["decls"]],
                         [("SD#2.2.2", None), ("Servo", "1.2.1")])
        # npm reads user/repo#ref as a GitHub repository, which is no registry name
        self.assertEqual(extract("```bash\nnpm install user/repo#v1 express\n```", "npm")["decls"][0]["name"],
                         "express")

    def test_espidf_file_that_yaml_cannot_read(self):
        # cut off at the token limit: the dependencies block is read up to the last line that
        # parses, nested keys (a version) are no entries
        cut = ('```yaml\nversion: "1.0.0"\ndependencies:\n  - name: "esp32"\n    version: ">=5.0.0"\n'
               '  - name: "esp_zigbee"\n    version: ">=1.0.0"\ntargets:\n  - "esp32::dri')
        r = extract(cut, "espidf")
        self.assertEqual([(d["name"], d["version"]) for d in r["decls"]], [("esp32", ">=5.0.0"), ("esp_zigbee", ">=1.0.0")])
        self.assertTrue(all("malformed_manifest" in d["flags"] for d in r["decls"]))
        mapping = ('```yaml\ndependencies:\n  driver:\n    version: "^1.0.0"  # SPI driver\n  esp_timer:\n'
                   '    version\n```\nmore: [')
        r = extract(mapping, "espidf")
        self.assertEqual([(d["name"], d["version"], d["flags"]) for d in r["decls"]],
                         [("driver", "^1.0.0", ["implicit_namespace"]), ("esp_timer", "version", ["implicit_namespace"])])

    def test_espidf_list_items_with_versions(self):
        t = ("```yaml\ndependencies:\n  - espressif/esp-modbus:^1.0.0\n  - esp32-camera@2.0.0\n  - bme280 >= 1.0.0\n"
             "  - registry:esp_j1939\n  - esp-idf_components::esp_gfx\n```")
        r = extract(t, "espidf")
        self.assertEqual([(d["owner"], d["name"], d["version"]) for d in r["decls"]],
                         [("espressif", "esp-modbus", "^1.0.0"), ("espressif", "esp32-camera", "2.0.0"),
                          ("espressif", "bme280", ">= 1.0.0"), ("espressif", "registry:esp_j1939", None),
                          ("espressif", "esp-idf_components::esp_gfx", None)])

    def test_idf_py_add_dependency_reads_names_as_the_component_manager(self):
        t = '```bash\nidf.py add-dependency "espressif/modbus@~1.2.0"\nidf.py add-dependency esp_eth_w5500^1.0\n```'
        self.assertEqual([(d["owner"], d["name"]) for d in extract(t, "espidf")["decls"]],
                         [("espressif", "modbus"), ("espressif", "esp_eth_w5500")])

    def test_unfenced_requirements_in_a_list(self):
        t = ("Steps:\n\n2. **Create a `requirements.txt`**:\n   - `pymqtt==1.5.0`\n   - `pyserial==3.4.0`\n\n"
             "3. **Run** `pip install -r requirements.txt`.\n")
        self.assertEqual([(d["name"], d["via"]) for d in extract(t, "pypi")["decls"]],
                         [("pymqtt", "unfenced requirements.txt"), ("pyserial", "unfenced requirements.txt")])
        # a list item that is no single code span, or a sentence about the file, is no requirement list
        prose = "Install the packages listed in requirements.txt:\n- `pip install -r requirements.txt`\n- flask\n"
        self.assertEqual(extract(prose, "pypi")["decls"], [])


class SecondPreScreenReview(unittest.TestCase):
    """Readings settled while every changed declaration was reviewed (change log 2026-10-28)."""

    def test_espidf_block_with_items_at_the_first_column(self):
        t = "```yaml\ndependencies:\n- espressif/led_strip^2.0\n\n- name: esp_modem\ntargets: [\n```"
        r = extract(t, "espidf")
        self.assertEqual([(d["owner"], d["name"], d["version"]) for d in r["decls"]],
                         [("espressif", "led_strip", "^2.0"), ("espressif", "esp_modem", None)])

    def test_idf_py_options_git_and_versions(self):
        t = ("```bash\nidf.py add-dependency --path main espressif/led_strip\n"
             "idf.py add-dependency esp_zigbee 1.0.0\n"
             "idf.py add-dependency --git https://github.com/x/mycomp.git mycomp\n"
             "idf.py add-dependency github.com/x/sensor\n```")
        got = [(d["owner"], d["name"], d["kind"]) for d in extract(t, "espidf")["decls"]]
        self.assertEqual(got, [("espressif", "led_strip", "registry"), ("espressif", "esp_zigbee", "registry"),
                               (None, "mycomp", "url"), (None, "github.com/x/sensor", "url")])


class ThirdPreScreenFixes(unittest.TestCase):
    """Gaps that the third model pre-screen of the study sample pointed to (change log 2026-10-28)."""

    def test_espidf_items_named_by_component(self):
        t = ("```yaml\nidf_component:\n  dependencies:\n    - component: esp_eth\n      uri: https://github.com/x/esp-eth.git\n"
             "      revision: v4.3\n    - idf_component: esp-zigbee\n      required: true\n```")
        r = extract(t, "espidf")
        self.assertEqual([(d["owner"], d["name"], d["kind"]) for d in r["decls"]],
                         [(None, "esp_eth", "url"), ("espressif", "esp-zigbee", "registry")])
        self.assertTrue(all("malformed_manifest" in d["flags"] for d in r["decls"]))

    def test_espidf_dependencies_nested_in_components(self):
        t = ("```yaml\nname: app\ncomponents:\n  - name: esp-http-server\n    source_url: https://github.com/x/y.git\n"
             "    dependencies:\n      - esp-http-server-common\n      - esp-idf\n```")
        r = extract(t, "espidf")
        self.assertEqual([(d["owner"], d["name"]) for d in r["decls"]],
                         [("espressif", "esp-http-server-common"), ("espressif", "esp-idf")])

    def test_command_ending_in_dot_git_is_no_url(self):
        t = "Install it from GitHub:\n\n```bash\npip install git+https://github.com/x/aiobacnet.git\n```"
        self.assertEqual([(d["name"], d["kind"]) for d in extract(t, "pypi")["decls"]],
                         [("git+https://github.com/x/aiobacnet.git", "url")])

    def test_install_of_the_current_directory(self):
        t = "```bash\ngit clone https://github.com/x/pyS7.git\ncd pyS7\npip install .\n```"
        self.assertEqual([(d["name"], d["kind"]) for d in extract(t, "pypi")["decls"]], [(".", "path")])


class OpenRegistryNameRules(unittest.TestCase):
    def test_crates_pypi_npm(self):
        from phantomdeps.classify import squattable
        sq = lambda eco, n, sub=None: squattable({"eco": eco, "name": n, "subtype": sub})  # noqa: E731
        self.assertEqual(sq("crates", "embedded-can-driver"), "open")
        self.assertEqual(sq("crates", "2d-graphics"), "blocked")         # must start with a letter
        self.assertEqual(sq("crates", "a" * 65), "blocked")              # at most 64 characters
        self.assertEqual(sq("crates", "can.bus"), "blocked")             # letters, digits, '-', '_'
        self.assertEqual(sq("crates", "proc-macro"), "blocked")          # reserved (canonical proc_macro)
        self.assertEqual(sq("pypi", "py-can"), "open")
        self.assertEqual(sq("pypi", "py-can-"), "blocked")               # PEP 508: ends with a letter or digit
        self.assertEqual(sq("npm", "node-red-contrib-x"), "open")
        self.assertEqual(sq("npm", "_private"), "blocked")               # no leading '_' without a scope
        self.assertEqual(sq("npm", "my pkg"), "blocked")                 # URL-safe characters only
        self.assertEqual(sq("npm", "@scope/pkg"), "namespace")


class ArduinoNameRules(unittest.TestCase):
    def test_registrability(self):
        from phantomdeps.classify import squattable
        sq = lambda n: squattable({"eco": "arduino", "name": n})  # noqa: E731
        self.assertEqual(sq("SD#2.2.2"), "blocked")            # '#' is no allowed character
        self.assertEqual(sq("_hidden"), "blocked")             # must start with a letter or digit
        self.assertEqual(sq("1234"), "blocked")                # must hold a letter
        self.assertEqual(sq("ArduinoMqttPlus"), "vendor-namespace")   # reserved for official libraries
        self.assertEqual(sq("arduino-lmic"), "vendor-namespace")      # in any case (arduino-lint LP012)
        self.assertEqual(sq("Adafruit Foo Library"), "open")
        self.assertEqual(sq("My" + "Lib" * 20 + "XY"), "blocked")     # 64 characters, the limit is 63
        self.assertEqual(sq("My" + "Lib" * 20 + "X"), "open")
        self.assertEqual(sq("2nd.Gen-Sensor_1"), "open")
        from phantomdeps.classify import squattable
        variant = lambda n, alt: squattable({"eco": "arduino", "name": n, "subtype": "separator_variant",  # noqa: E731
                                             "alt_canonical": alt})
        self.assertEqual(variant("mcp_can", "MCP_CAN"), "blocked")      # equal regardless of case
        self.assertEqual(variant("MCP CAN", "MCP_CAN"), "variant")      # separators: not tested


class PlatformioEspNameRules(unittest.TestCase):
    def test_platformio(self):
        from phantomdeps.classify import squattable
        sq = lambda n, owner=None: squattable({"eco": "platformio", "name": n, "owner": owner})  # noqa: E731
        self.assertEqual(sq("TinyCAN-Lite"), "open")
        self.assertEqual(sq("TinyCAN-Lite", "someone"), "namespace")
        self.assertEqual(sq("MakerAsiaFoundation:Mavlink"), "blocked")         # ':' is refused
        self.assertEqual(sq("SDFat/examples/Teensy41", "bolderflight"), "blocked")   # '/' in the name
        self.assertEqual(sq("esp32-" * 17), "blocked")                         # 102 characters, at most 100
        self.assertEqual(sq("a" * 100), "open")

    def test_esp(self):
        from phantomdeps.classify import squattable
        sq = lambda n, owner="espressif": squattable({"eco": "espidf", "name": n, "owner": owner})  # noqa: E731
        self.assertEqual(sq("esp_lcd_ili9341"), "vendor-namespace")
        self.assertEqual(sq("ili9341", "someone"), "namespace")
        self.assertEqual(sq("idf_component_registry:esp-idf-json"), "blocked")   # ':' breaks the slug
        self.assertEqual(sq("esp-esp32s3-devkitc-"), "blocked")                 # ends with a separator
        self.assertEqual(sq("display/ssd1306", "esp-idf-components"), "blocked")  # a second '/'
        self.assertEqual(sq("esp__x"), "blocked")                               # two underscores
        self.assertEqual(sq("idf-v4.x-framework"), "blocked")                   # '.' is no slug character

    def test_esp_lookup_of_a_name_the_manager_refuses(self):
        # the API answers a path with a second '/' with 405; the name cannot exist, so it is absent
        # (before, the lookup failed every time and the declaration stayed unresolved)
        from unittest import mock
        from phantomdeps import registries as R
        esp = R.EspRegistry.__new__(R.EspRegistry)
        esp.cache, esp.offline, esp.latencies, esp.delay = {}, False, [], 0

        class Cache(dict):
            def put(self, k, v):
                self[k] = v
        esp.cache = Cache()
        answer = mock.Mock(status=405, error=None)
        with mock.patch.object(R, "http_get", return_value=answer):
            self.assertEqual(esp.lookup("esp-idf-components", "display/ssd1306")["status"], "not_found")
            # a valid name whose lookup fails stays a failure (retried later)
            self.assertEqual(esp.lookup("espressif", "led_strip")["status"], "error")


if __name__ == "__main__":
    unittest.main()
