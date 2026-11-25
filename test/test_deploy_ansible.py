"""The Ansible deploy (deploy/ansible) must stay in step with sh/deploy-data
and sh/deploy-apps while both exist: same .env key spec, same rsync excludes,
same service lists. A mismatch here would make the two tools deploy
differently (e.g. an exclude missing from one side and a --delete sync wipes
LOGINS.md or deploy/.env backups on the server, or a secret generated with
another kind). Pure file parsing; no services needed."""

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "deploy" / "ansible"


def sh_env_spec(script, override):
    text = (ROOT / "sh" / script).read_text()
    body = re.search(r'^ENV_SPEC="(.*?)"$', text, re.S | re.M).group(1)
    return [line.split() for line in body.replace("$OVERRIDE", override).splitlines() if line.strip()]


def sh_array(path, name):
    text = (ROOT / path).read_text()
    body = re.search(rf"^{name}=\((.*?)\)", text, re.S | re.M).group(1)
    items = []
    for line in body.splitlines():
        line = line.split(" #", 1)[0].strip()
        items.extend(line.split())
    return items


def unquote(s):
    return s[1:-1] if len(s) > 1 and s[0] == s[-1] and s[0] in "'\"" else s


@unittest.skipIf(yaml is None, "pyyaml not installed")
class TestAnsibleMatchesShellDeploy(unittest.TestCase):
    def group_vars(self, name):
        return yaml.safe_load((ANSIBLE / "group_vars" / f"{name}.yml").read_text())

    def ansible_env_spec(self, name):
        gv = self.group_vars(name)
        spec = gv["env_spec"].replace("{{ compose_override }}", gv["compose_override"])
        return [line.split() for line in spec.splitlines() if line.strip()]

    def test_data_box_env_spec(self):
        self.assertEqual(self.ansible_env_spec("databox"),
                         sh_env_spec("deploy-data", "docker-compose.databox.yml"))

    def test_apps_box_env_spec(self):
        self.assertEqual(self.ansible_env_spec("appsbox"),
                         sh_env_spec("deploy-apps", "docker-compose.appsbox.yml"))

    def test_rsync_excludes(self):
        sh = [unquote(a.split("=", 1)[1]) for a in sh_array("sh/lib/deploy-common.sh", "RSYNC_EXCLUDES")]
        self.assertEqual(self.group_vars("all")["rsync_excludes"], sh)
        # the server-only files a --delete sync must never touch
        for path in ("/LOGINS.md", "/deploy/.env", "/deploy/.env.bak*", "/deploy/rclone/*.conf"):
            self.assertIn(path, sh)

    def test_max_delete(self):
        text = (ROOT / "sh/lib/deploy-common.sh").read_text()
        sh = int(re.search(r"^RSYNC_MAX_DELETE=(\d+)", text, re.M).group(1))
        self.assertEqual(self.group_vars("all")["rsync_max_delete"], sh)

    def test_service_lists(self):
        self.assertEqual(self.group_vars("appsbox")["app_services"],
                         sh_array("sh/deploy-apps", "APP_SERVICES"))
        self.assertEqual(self.group_vars("databox")["airflow_services"],
                         sh_array("sh/deploy-data", "AIRFLOW_SERVICES"))

    def test_role_scripts_exist(self):
        for script in self.group_vars("databox")["role_scripts"]:
            self.assertTrue((ROOT / "deploy/db/init" / script["file"]).is_file(), script["file"])


if __name__ == "__main__":
    unittest.main()
