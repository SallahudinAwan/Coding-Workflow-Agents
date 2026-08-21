import unittest

from code_agent.developer_agent import DeveloperIntent, classify_developer_request


class DeveloperAgentRouterTests(unittest.TestCase):
    def test_routes_code_change_to_code_agent(self):
        self.assertEqual(
            classify_developer_request("Fix the login validation and add tests"),
            DeveloperIntent.CODE,
        )

    def test_routes_publish_request_to_github_agent(self):
        self.assertEqual(
            classify_developer_request("Push these changes to GitHub"),
            DeveloperIntent.GITHUB,
        )
        self.assertEqual(
            classify_developer_request("Create a pull request"),
            DeveloperIntent.GITHUB,
        )

    def test_routes_mixed_request_in_sequence(self):
        self.assertEqual(
            classify_developer_request("Fix the bug and push the changes to GitHub"),
            DeveloperIntent.CODE_AND_GITHUB,
        )

    def test_refuses_github_operations_outside_the_agent_scope(self):
        self.assertEqual(
            classify_developer_request("Merge pull request 42"),
            DeveloperIntent.GITHUB_UNSUPPORTED,
        )
        self.assertEqual(
            classify_developer_request("Close issue 12"),
            DeveloperIntent.GITHUB_UNSUPPORTED,
        )


if __name__ == "__main__":
    unittest.main()
