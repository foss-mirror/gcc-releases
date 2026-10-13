# SPDX-License-Identifier: GPL-3.0-or-later
"""Check GitHub pagination, credentials, and remote absence handling."""

import io
import os
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import github_api


class GitHubApiTests(unittest.TestCase):
    def test_authenticated_get_uses_the_fixed_origin_and_bounded_timeout(self) -> None:
        with (
            patch.dict(os.environ, {"GH_TOKEN": "test-token"}),
            patch("github_api.urllib.request.urlopen", return_value=io.BytesIO(b'{"id": 12}')) as request,
        ):
            self.assertEqual(github_api.get("owner/repo", "releases/12"), {"id": 12})
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.github.com/repos/owner/repo/releases/12")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.call_args.kwargs["timeout"], 30)

    def test_only_explicitly_allowed_404_is_treated_as_absent(self) -> None:
        for status in (401, 403, 404, 429, 500):
            for absent_ok in (False, True):
                error = urllib.error.HTTPError("https://api.github.com/", status, "fixture failure", Message(), None)
                with (
                    self.subTest(status=status, absent_ok=absent_ok),
                    patch("github_api.urllib.request.urlopen", side_effect=error),
                ):
                    if status == 404 and absent_ok:
                        self.assertIsNone(github_api.get("owner/repo", "releases/12", absent_ok=True))
                    else:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            github_api.get("owner/repo", "releases/12", absent_ok=absent_ok)
                        self.assertIs(caught.exception, error)

    def test_release_listing_paginates(self) -> None:
        batch = [{"id": number} for number in range(100)]
        with patch.object(github_api, "get", side_effect=[batch, [{"id": 100}]]) as get:
            self.assertEqual(list(github_api.releases("owner/repo")), [*batch, {"id": 100}])
        self.assertEqual(get.call_args.args[1], "releases?per_page=100&page=2")
