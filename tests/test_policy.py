import unittest
from policy import authorize
from store import RuleError


class PermissionTests(unittest.TestCase):
    def args(self, **changes):
        args = dict(guild_id=1, expected_guild_id=1, is_human_member=True,
                    is_admin=False, role_ids={30}, channel_id=10,
                    settings=dict(channel_id=10, officer_role_id=20, member_role_id=30))
        args.update(changes)
        return args

    def test_member_can_participate_but_cannot_issue_points(self):
        authorize(**self.args())
        with self.assertRaises(RuleError):
            authorize(**self.args(officer=True))

    def test_officer_and_admin_can_issue_points(self):
        authorize(**self.args(role_ids={20}, officer=True))
        authorize(**self.args(role_ids=set(), is_admin=True, officer=True))

    def test_officer_cannot_change_configuration(self):
        with self.assertRaises(RuleError):
            authorize(**self.args(role_ids={20}, admin=True, require_setup=False))
        authorize(**self.args(is_admin=True, admin=True, require_setup=False, settings=None))

    def test_wrong_guild_dm_bot_and_wrong_channel_rejected(self):
        for change in (dict(guild_id=2), dict(guild_id=None), dict(is_human_member=False), dict(channel_id=11)):
            with self.subTest(change=change), self.assertRaises(RuleError):
                authorize(**self.args(is_admin=True, **change))

    def test_removed_member_or_officer_role_takes_effect(self):
        with self.assertRaises(RuleError):
            authorize(**self.args(role_ids=set()))
        with self.assertRaises(RuleError):
            authorize(**self.args(role_ids={30}, officer=True))

    def test_unconfigured_guild_rejects_normal_commands(self):
        with self.assertRaises(RuleError):
            authorize(**self.args(settings=None))


if __name__ == "__main__":
    unittest.main()
