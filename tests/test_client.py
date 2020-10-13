# This file is part of the ops-lib-pgsql component for Juju Operator
# Framework Charms.
# Copyright 2020 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the Lesser GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the Lesser GNU General Public License for more details.
#
# You should have received a copy of the Lesser GNU General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.

from textwrap import dedent
import unittest
from unittest.mock import patch

import ops.charm
import ops.lib
import ops.testing

from pgsql import client, ConnectionString


class Charm(ops.charm.CharmBase):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.db = client.PostgreSQLClient(self, "db")
        self.framework.observe(self.db.on.database_relation_joined, self.on_database_relation_joined)

    database_relation_joined_event = None

    def on_database_relation_joined(self, ev):
        self.database_relation_joined_event = ev


class TestPGSQLBase(unittest.TestCase):
    def setUp(self):
        # TODO: Operator Framework should expose leadership, and be mocked there.
        self.leadership_data = {}
        leader_patch = unittest.mock.patch.multiple(
            client,
            _get_pgsql_leader_data=self.leadership_data.copy,
            _set_pgsql_leader_data=self.leadership_data.update,
        )
        leader_patch.start()
        self.addCleanup(leader_patch.stop)

        meta = dedent(
            """\
            name: pgclient
            requires:
              db:
                interface: pgsql
                limit: 1
            """
        )
        self.harness = ops.testing.Harness(Charm, meta=meta)
        self.addCleanup(self.harness.cleanup)
        self.relation_id = self.harness.add_relation("db", "postgresql")
        self.remote_app_name = "postgresql"
        self.remote_unit_names = [f"{self.remote_app_name}/{i}" for i in range(3, 5)]
        for n in self.remote_unit_names:
            self.harness.add_relation_unit(self.relation_id, n)

        self.harness.begin_with_initial_hooks()

        self.ev = self.harness.charm.database_relation_joined_event
        self.relation = self.harness.model.relations["db"][0]
        self.log = self.harness.charm.db.log
        self.local_unit = self.harness.model.unit
        self.remote_app = self.ev.app
        self.remote_units = sorted((u for u in self.relation.units if u.app == self.remote_app), key=lambda x: x.name)


class TestPGSQLHarness(TestPGSQLBase):
    def test_leadership_mock(self):
        self.leadership_data["foo"] = "bar"
        self.assertEqual(client._get_pgsql_leader_data(), self.leadership_data)
        self.assertIsNot(client._get_pgsql_leader_data(), self.leadership_data)

        client._set_pgsql_leader_data({"one": "two"})
        self.assertEqual(client._get_pgsql_leader_data(), {"foo": "bar", "one": "two"})

        client._set_pgsql_leader_data({"foo": "baz"})
        self.assertEqual(client._get_pgsql_leader_data(), {"foo": "baz", "one": "two"})

        self.assertEqual(self.leadership_data, {"foo": "baz", "one": "two"})


class TestPGSQLHelpers(TestPGSQLBase):
    def setUp(self):
        super().setUp()
        self.harness.disable_hooks()

    @patch("pgsql.client._is_ready")
    def test_master_unset(self, is_ready):
        # Master helper returns None when no relation data is set.
        is_ready.return_value = True

        # No connection details present on relation, yet.
        self.assertIsNone(client._master(self.log, self.relation, self.local_unit))
        self.assertFalse(is_ready.called)

    @patch("pgsql.client._is_ready")
    def test_master_unready(self, is_ready):
        # Master helper returns None when relation app data is set but not yet ready.
        is_ready.return_value = False
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_app_name, rd)

        self.assertIsNone(client._master(self.log, self.relation, self.local_unit))
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_app],
        )

    @patch("pgsql.client._is_ready")
    def test_master_ready(self, is_ready):
        # Master helper returns connection string when relation app data is set and ready.
        is_ready.return_value = True
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_app_name, rd)

        self.assertEqual(client._master(self.log, self.relation, self.local_unit), rd["master"])
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_app],
        )

    @patch("pgsql.client._is_ready")
    def test_master_legacy(self, is_ready):
        # Ensure we fall back to using unit relation data if the app relation data is unset.
        is_ready.return_value = True
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_unit_names[1], rd)

        self.assertEqual(client._master(self.log, self.relation, self.local_unit), rd["master"])
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_units[1]],
        )

    @patch("pgsql.client._is_ready")
    def test_standbys_unset(self, is_ready):
        # Standbys helper returns None when no relation data is set.
        is_ready.return_value = True

        # No connection details present on relation, yet.
        self.assertEqual(client._standbys(self.log, self.relation, self.local_unit), [])
        self.assertFalse(is_ready.called)

    @patch("pgsql.client._is_ready")
    def test_standbys_unready(self, is_ready):
        # Standbys helper returns None when relation app data is set but not yet ready.
        is_ready.return_value = False
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_app_name, rd)

        self.assertEqual(client._standbys(self.log, self.relation, self.local_unit), [])
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_app],
        )

    @patch("pgsql.client._is_ready")
    def test_standbys_ready(self, is_ready):
        # Master helper returns connection string when relation app data is set and ready.
        is_ready.return_value = True
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_app_name, rd)

        self.assertEqual(client._standbys(self.log, self.relation, self.local_unit), standbys)
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_app],
        )

    @patch("pgsql.client._is_ready")
    def test_standbys_legacy(self, is_ready):
        # Ensure we fall back to using unit relation data if the app relation data is unset.
        is_ready.return_value = True
        standbys = ["host=standby1", "host=standby2"]
        rd = {"master": "host=master", "standbys": "\n".join(standbys)}
        self.harness.update_relation_data(self.relation_id, self.remote_unit_names[1], rd)

        self.assertEqual(client._standbys(self.log, self.relation, self.local_unit), standbys)
        self.assertTrue(is_ready.called)
        is_ready.assert_called_once_with(
            self.log,
            self.leadership_data,
            self.relation.data[self.local_unit],
            self.relation.data[self.remote_units[1]],
        )

    def test_is_ready_no_egress(self):
        # The relation is considered ready if the client has published
        # no egress-subnets. This unexpected result is to support old
        # versions of Juju that predate cross-model relation support.
        # This should not happen with supported Juju versions.
        self.assertTrue(client._is_ready(self.log, {}, {}, {}))
        self.assertTrue(client._is_ready(self.log, {}, {}, {"allowed-subnets": "127.23.0.0/24"}))

    def test_is_ready_no_allowed(self):
        # The relation is not ready if allowed-subnets does not contain our egress-subnets.
        # The remote end has not yet granted the local unit access.
        self.assertFalse(client._is_ready(self.log, {}, {"egress-subnets": "127.23.0.0/24"}, {}))
        self.assertFalse(
            client._is_ready(self.log, {}, {"egress-subnets": "127.23.0.0/24"}, {"allowed-subnets": "127.0.1/24"})
        )

    def test_is_ready_defaults(self):
        # allowed-subnets grants access, and default database settings requested.
        self.assertTrue(
            client._is_ready(
                self.log, {}, {"egress-subnets": "127.23.1.0/24"}, {"allowed-subnets": "127.23.0.0/24,127.23.1.0/24"}
            )
        )

    def test_is_ready_mismatch(self):
        # The relation is not ready if database settings (such as the
        # database name) have not been mirrored back.
        for k in ["database", "roles", "extensions"]:
            with self.subTest(f"{k} mismatch"):
                # Requested setting should be available in application
                # shared data. This could be leadership data or a peer
                # relation application databag.
                self.assertFalse(
                    client._is_ready(
                        self.log,
                        {k: "value"},
                        {"egress-subnets": "127.23.0.0/24"},
                        {"allowed-subnets": "127.23.1.0/24"},
                    )
                )
                self.assertFalse(
                    client._is_ready(
                        self.log,
                        {k: "value"},
                        {"egress-subnets": "127.23.0.0/24"},
                        {"allowed-subnets": "127.23.1.0/24", k: "different"},
                    )
                )

    def test_is_ready_match(self):
        # The relation is ready if its egress has been allowed access and its
        # settings have been mirrored back, indicating they have been applied.
        app = {}
        loc = {"egress-subnets": "127.0.0.0/24"}
        rel = {"allowed-subnets": "127.0.0.0/24"}
        for k in ["database", "roles", "extensions"]:
            with self.subTest(f"{k} match"):
                # Requested setting should be available in application
                # shared data. This could be leadership data or a peer
                # relation application databag.
                app[k] = "value"
                self.assertFalse(client._is_ready(self.log, app, loc, rel))
                rel[k] = "value"
                self.assertTrue(client._is_ready(self.log, app, loc, rel))


class TestPostgreSQLRelationEvent(TestPGSQLBase):
    @patch("pgsql.client._master")
    def test_master(self, master):
        c = "host=master dbname=foo"
        master.return_value = c
        self.assertEqual(self.ev.master, ConnectionString(c))
        master.assert_called_once_with(self.ev.log, self.relation, self.local_unit)

    @patch("pgsql.client._standbys")
    def test_standbys(self, standbys):
        c1 = "host=standby1 dbname=foo"
        c2 = "host=standby2 dbname=foo"
        standbys.return_value = [c1, c2]
        self.assertEqual(self.ev.standbys, [ConnectionString(c1), ConnectionString(c2)])
        standbys.assert_called_once_with(self.ev.log, self.relation, self.local_unit)
