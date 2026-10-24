import json
import subprocess
import unittest
from unittest.mock import patch

import vercel_list


class VercelListTests(unittest.TestCase):
    def test_pagination_and_production_only(self):
        pages = [
            {"contextName": "team", "projects": [{"id": "one", "name": "App", "latestProductionUrl": "https://app.vercel.app"}], "pagination": {"next": 123}},
            {"projects": [{"id": "two", "name": "Pending", "latestProductionUrl": None}], "pagination": {"next": None}},
        ]
        with patch.object(vercel_list, "cli_command", return_value=["node", "cli.js"]), patch.object(vercel_list.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, '<hint/>\n'+json.dumps(p), '') for p in pages]) as run:
            messages = vercel_list.list_messages("team")
        self.assertIn("--next", run.call_args_list[1].args[0])
        self.assertIn("123", run.call_args_list[1].args[0])
        text = "".join(messages)
        self.assertIn("1 proyectos publicados", text)
        self.assertIn("https://app.vercel.app", text)
        self.assertIn("sin publicación", text)

    def test_no_duplicate_projects_or_private_query(self):
        p = {"id": "same", "name": "App", "latestProductionUrl": "https://app.vercel.app?key=secret"}
        text = "".join(vercel_list.format_projects("team", [p, p]))
        self.assertIn("1 proyectos publicados", text)
        self.assertNotIn("secret", text)
        self.assertEqual(text.count("https://app.vercel.app"), 1)

    def test_telegram_chunks_preserve_whole_links(self):
        projects = [{"id": str(i), "name": f"app-{i}", "latestProductionUrl": f"https://app-{i}.vercel.app"} for i in range(200)]
        messages = vercel_list.format_projects("team", projects)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= 3900 for m in messages))
        for i in range(200):
            self.assertTrue(any(f"https://app-{i}.vercel.app" in m for m in messages))

    def test_login_error_without_raw_output(self):
        with patch.object(vercel_list, "cli_command", return_value=["cli"]), patch.object(vercel_list.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '', 'Unauthorized token SECRET')):
            with self.assertRaises(vercel_list.VercelListError) as exc:
                vercel_list.list_messages()
        self.assertIn("sesión", str(exc.exception))
        self.assertNotIn("SECRET", str(exc.exception))

    def test_timeout(self):
        with patch.object(vercel_list, "cli_command", return_value=["cli"]), patch.object(vercel_list.subprocess, "run", side_effect=subprocess.TimeoutExpired('cli',30)):
            with self.assertRaisesRegex(vercel_list.VercelListError, "tardando"):
                vercel_list.list_messages()


if __name__ == '__main__':
    unittest.main()
