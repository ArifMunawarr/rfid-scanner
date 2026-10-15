import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


class StopListener(BaseException):
    pass


class MultiReaderTests(unittest.TestCase):
    def setUp(self):
        config = {
            "RFID_MODE": "server",
            "RFID_PORT": "6000",
            "RFID_READER_1_HOST": "127.0.0.2",
            "RFID_READER_2_HOST": "127.0.0.3",
        }
        self.env = patch.dict(os.environ, config, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location(
            "rfid_under_test", Path(__file__).with_name("rfid.py")
        )
        self.rfid = importlib.util.module_from_spec(spec)
        with patch("dotenv.load_dotenv", return_value=False):
            spec.loader.exec_module(self.rfid)
        self.frame = bytes.fromhex("BB F1 0B 80 05 26 07 26 53 33 07 00 02 FF 62 0D 0A")

    def test_loads_two_readers_on_shared_port(self):
        self.assertEqual(self.rfid.NUM_READERS, 2)
        self.assertEqual([r["port"] for r in self.rfid.READERS], [6000, 6000])

    def test_starts_one_listener_per_port(self):
        with patch.object(self.rfid.threading, "Thread") as thread:
            self.rfid.tcp_server()
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["target"], self.rfid._tcp_listener)
        self.assertEqual(thread.call_args.kwargs["args"], (6000, self.rfid.READERS))

    def test_distinct_ports_start_distinct_listeners(self):
        self.rfid.READERS[1]["port"] = 6001
        with patch.object(self.rfid.threading, "Thread") as thread:
            self.rfid.tcp_server()
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(
            [call.kwargs["args"][0] for call in thread.call_args_list], [6000, 6001]
        )

    def test_routes_by_ip_rejects_unknown_and_duplicate_connections(self):
        first, second, unknown, duplicate = [MagicMock() for _ in range(4)]
        server = MagicMock()
        server.accept.side_effect = [
            (unknown, ("127.0.0.4", 41000)),
            (second, ("127.0.0.3", 41001)),
            (first, ("127.0.0.2", 41002)),
            (duplicate, ("127.0.0.2", 41003)),
            StopListener(),
        ]
        with patch.object(self.rfid.socket, "socket") as make_socket:
            make_socket.return_value.__enter__.return_value = server
            with patch.object(self.rfid.threading, "Thread") as thread:
                with self.assertRaises(StopListener):
                    self.rfid._tcp_listener(6000, self.rfid.READERS)
        server.bind.assert_called_once_with(("0.0.0.0", 6000))
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(thread.call_args_list[0].kwargs["args"][0], 2)
        self.assertEqual(thread.call_args_list[1].kwargs["args"][0], 1)
        unknown.close.assert_called_once()
        duplicate.close.assert_called_once()
        self.assertIs(self.rfid.readers_state[1]["conn"], first)
        self.assertIs(self.rfid.readers_state[2]["conn"], second)

    def test_shared_tag_keeps_both_reader_ids_and_api_sees_updates(self):
        spec = importlib.util.spec_from_file_location(
            "routes_under_test", Path(__file__).with_name("routes.py")
        )
        routes = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", {"rfid": self.rfid}):
            spec.loader.exec_module(routes)
        parsed = self.rfid.parse_frame(self.frame)
        self.rfid._update_tag(1, parsed)
        self.rfid._update_tag(2, parsed)
        result = routes.view()
        self.assertEqual(result["total"], 2)
        self.assertEqual({t["reader_id"] for t in result["tags"]}, {1, 2})
        self.assertEqual(result["connection"]["active_tags"], 2)
        snapshot = self.rfid.get_tags()
        self.rfid._update_tag(1, parsed)
        self.assertEqual(snapshot[0]["count"], 1)
        self.assertEqual(routes.view()["tags"][0]["count"], 2)
        self.rfid.stop_scan()
        self.assertEqual(routes.view()["total"], 0)
        self.rfid._update_tag(2, parsed)
        self.assertEqual(routes.view()["total"], 0)
        self.rfid.start_scan()
        self.rfid._update_tag(2, parsed)
        self.assertEqual(routes.view()["total"], 1)

    def test_server_worker_reads_fragmented_frame_and_disconnects(self):
        conn = MagicMock()
        conn.recv.side_effect = [self.frame[:5], self.frame[5:], b""]
        self.rfid.readers_state[2]["conn"] = conn
        self.rfid._tcp_worker(2, "127.0.0.3", 6000, conn)
        state = self.rfid.readers_state[2]
        self.assertEqual(len(state["tags"]), 1)
        self.assertFalse(state["connection_status"]["connected"])
        self.assertIsNone(state["conn"])
        conn.close.assert_called_once()

    def test_listener_bind_error_sets_status_for_both_readers(self):
        server = MagicMock()
        server.bind.side_effect = OSError("Address already in use")
        with patch.object(self.rfid.socket, "socket") as make_socket:
            make_socket.return_value.__enter__.return_value = server
            with patch.object(self.rfid.time, "sleep", side_effect=StopListener):
                with self.assertRaises(StopListener):
                    self.rfid._tcp_listener(6000, self.rfid.READERS)
        for status in self.rfid.get_connection_status()["readers"]:
            self.assertEqual(status["state"], "error")
            self.assertIn("Address already in use", status["last_error"])

    def test_duplicate_reader_configuration_is_rejected(self):
        os.environ["RFID_READER_2_HOST"] = os.environ["RFID_READER_1_HOST"]
        with self.assertRaisesRegex(ValueError, "IP dan port"):
            self.rfid._load_readers()

    def test_commands_are_sent_to_both_readers(self):
        first, second = MagicMock(), MagicMock()
        self.rfid.readers_state[1]["conn"] = first
        self.rfid.readers_state[2]["conn"] = second
        self.assertTrue(self.rfid.stop_scan())
        self.assertTrue(self.rfid.start_scan())
        for conn in (first, second):
            self.assertEqual(conn.sendall.call_count, 2)
            self.assertEqual(
                conn.sendall.call_args.args[0], bytes.fromhex("BB 17 02 00 00 19 0D 0A")
            )

    def test_cleanup_uses_configured_timeout(self):
        self.rfid.TAG_ACTIVE_TIMEOUT = 10
        self.rfid._update_tag(1, self.rfid.parse_frame(self.frame))
        tag = next(iter(self.rfid.readers_state[1]["tags"].values()))
        with patch.object(self.rfid.time, "time", return_value=tag["last_time"] + 6):
            with patch.object(self.rfid.time, "sleep", side_effect=StopListener):
                with self.assertRaises(StopListener):
                    self.rfid.cleanup_tags()
        self.assertEqual(self.rfid.get_connection_status()["active_tags"], 1)
        with patch.object(self.rfid.time, "time", return_value=tag["last_time"] + 11):
            with patch.object(self.rfid.time, "sleep", side_effect=StopListener):
                with self.assertRaises(StopListener):
                    self.rfid.cleanup_tags()
        self.assertEqual(self.rfid.get_connection_status()["active_tags"], 0)

    def test_client_worker_reads_data_and_sends_start_command(self):
        self.rfid.MODE = "client"
        conn = MagicMock()
        conn.recv.side_effect = [self.frame, b""]
        with patch.object(self.rfid.socket, "create_connection", return_value=conn):
            with patch.object(self.rfid.time, "sleep", side_effect=StopListener):
                with self.assertRaises(StopListener):
                    self.rfid._tcp_worker(1, "127.0.0.2", 6000)
        conn.settimeout.assert_called_once_with(None)
        conn.sendall.assert_called_once_with(bytes.fromhex("BB 17 02 00 00 19 0D 0A"))
        self.assertEqual(len(self.rfid.readers_state[1]["tags"]), 1)
        conn.close.assert_called_once()

    def test_client_mode_still_starts_worker_per_reader(self):
        self.rfid.MODE = "client"
        with patch.object(self.rfid.threading, "Thread") as thread:
            self.rfid.tcp_server()
        self.assertEqual(thread.call_count, 2)
        for call in thread.call_args_list:
            self.assertEqual(call.kwargs["target"], self.rfid._tcp_worker)


if __name__ == "__main__":
    unittest.main()
