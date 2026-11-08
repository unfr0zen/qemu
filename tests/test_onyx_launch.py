"""Onyx launcher selection and process lifetime without firmware or NAND dumps."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("onyx_launch", REPO / "boards/onyx/launch.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class OnyxLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "boards/onyx").mkdir(parents=True)
        (self.repo / "qemu-src/build").mkdir(parents=True)
        (self.repo / ".qemu/onyx").mkdir(parents=True)
        (self.repo / "ui").mkdir()
        shutil.copy(REPO / "run.sh", self.repo / "run.sh")
        shutil.copy(REPO / "boards/onyx/launch.py", self.repo / "boards/onyx/launch.py")
        self.nand = self.repo / ".qemu/onyx/nand-state.bin"
        self.nand.write_bytes(b"synthetic NAND")
        self.qemu = self.repo / "qemu-src/build/qemu-system-arm"
        self.qemu.write_text(
            f"#!{sys.executable}\nimport json, os, pathlib, sys, time\n"
            "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            "pathlib.Path(os.environ['CAPTURE'] + '.pid').write_text(str(os.getpid()))\n"
            "time.sleep(float(os.environ.get('QEMU_WAIT', '0')))\n")
        self.qemu.chmod(0o755)
        self.capture = self.repo / "args.json"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('NAND', 'QEMU_', 'UI_PORT'))}
        self.env['CAPTURE'] = str(self.capture)

    def run_sh(self, *args, **env):
        return subprocess.run(["sh", str(self.repo / "run.sh"), *args],
                              env=self.env | env, capture_output=True, text=True, timeout=10)

    def test_host_gpu_selection(self):
        (self.repo / 'qemu-src/build-docker').symlink_to('build')
        tools = self.repo / 'bin'
        tools.mkdir()
        uname = tools / 'uname'
        uname.write_text('#!/bin/sh\nprintf "%s\\n" "$HOST_OS"\n')
        uname.chmod(0o755)
        docker = tools / 'docker'
        docker.write_text('#!/bin/sh\nif [ "$1" = run ]; then\n'
                          '    exec "$FAKE_QEMU" "$@"\nfi\n')
        docker.chmod(0o755)
        (self.repo / 'docker').mkdir()
        (self.repo / 'docker/Dockerfile').write_text('FROM scratch\n')
        env = {'PATH': str(tools) + os.pathsep + self.env['PATH'],
               'NAND_STATE': str(self.nand), 'FAKE_QEMU': str(self.qemu)}
        cases = [
            ('Linux', 'diamond3', (), False, True),
            ('Darwin', 'diamond3', (), False, True),
            ('FreeBSD', 'diamond3', (), False, False),
            ('Linux', 'diamond3', ('--no-host-gpu',), False, False),
            ('Darwin', 'diamond3', ('--no-host-gpu',), False, False),
            ('FreeBSD', 'diamond3', ('--host-gpu',), False, True),
            ('Linux', 'diamond3', ('--host-gpu', '--no-host-gpu'), False, False),
            ('Linux', 'diamond3', ('--no-host-gpu', '--host-gpu'), False, True),
            ('Linux', 'j49', (), False, False),
            ('Linux', 'diamond3', (), True, False),
            ('Darwin', 'diamond3', (), True, False),
            ('Linux', 'diamond3', ('--host-gpu',), True, True),
        ]
        for host, machine, options, container, enabled in cases:
            with self.subTest(host=host, machine=machine, options=options,
                              docker=container):
                result = self.run_sh('--machine', machine, '--no-provision',
                                     '--no-usb', *options, **env, HOST_OS=host,
                                     QEMU_FORCE_DOCKER='1' if container else '0')
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(self.capture.read_text())
                self.assertEqual('vivante-gc.host-gpu=on' in args, enabled)
                self.assertNotIn('--host-gpu', args)
                self.assertNotIn('--no-host-gpu', args)

    def test_default_nand_and_qemu_options(self):
        result = self.run_sh("--onyx", "-d", "guest_errors")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertEqual(args[:4], ['-M', 'onyx', '-m', '128M'])
        self.assertIn(f'if=none,id=nand,format=raw,file={self.nand},snapshot=off', args)
        self.assertIn('-nographic', args)
        self.assertEqual(args[args.index('-watchdog-action') + 1], 'none')
        self.assertEqual(args[args.index('-parallel') + 1], 'none')
        self.assertEqual(args[-2:], ['-d', 'guest_errors'])
        self.assertNotIn('-bios', args)
        self.assertNotIn('vivante-gc.host-gpu=on', args)
        self.assertIn('fsl-imx6ul.usb0-hostchar=onyxusb', args)
        devices = [args[index + 1] for index, arg in enumerate(args) if arg == '-chardev']
        self.assertTrue(any(device.startswith('socket,id=onyxusb,') for device in devices))

    def test_machine_alias_and_seed(self):
        self.nand.unlink()
        source = self.repo / 'source.bin'
        source.write_bytes(b'synthetic seed')
        result = self.run_sh('--machine', 'onyx', '--nand', str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.nand.read_bytes(), source.read_bytes())
        self.assertFalse(Path(str(self.nand) + '.backplate').exists())

    def test_snapshot_and_icount(self):
        result = self.run_sh('--onyx', '--nand', str(self.nand),
                             NAND_SNAPSHOT='1', QEMU_ICOUNT='shift=3')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())
        self.assertIn(f'if=none,id=nand,format=raw,file={self.nand.parent / "nand-snapshot.bin"},snapshot=on', args)
        self.assertEqual(args[args.index('-icount') + 1], 'shift=3')

    def test_unsupported_options_and_conflicting_machine(self):
        for option in ('--usb', '--cloud', '--root1', '--gdb', '--j49'):
            with self.subTest(option=option):
                result = self.run_sh('--onyx', option)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_missing_nand(self):
        self.nand.unlink()
        result = self.run_sh('--onyx')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--onyx --nand FILE', result.stderr)

    def test_ui_shutdown_stops_qemu(self):
        ui_pid = self.repo / 'ui.pid'
        (self.repo / 'ui/ui.py').write_text(
            "import json, os, pathlib, sys, time\n"
            f"pathlib.Path({str(self.repo / 'ui-args.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
            f"pathlib.Path({str(ui_pid)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n")
        process = subprocess.Popen(
            ['sh', str(self.repo / 'run.sh'), '--onyx', '--ui', '--ui-port', '18075'],
            env=self.env | {'QEMU_WAIT': '60'}, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (ui_pid.exists() and self.capture.exists()):
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            args = json.loads(self.capture.read_text())
            self.assertIn('-qmp', args)
            self.assertIn('chardev:con', args)
            devices = [args[index + 1] for index, arg in enumerate(args) if arg == '-chardev']
            console = next(device for device in devices if device.startswith('socket,id=con,'))
            self.assertIn('server=on,wait=on,nodelay=on', console)
            board = next(device for device in devices if device.startswith('socket,id=boardui,'))
            self.assertIn('server=on,wait=off', board)
            self.assertIn('driver=flintstone.board,property=chardev,value=boardui', args)
            self.assertNotIn('-nographic', args)
            ui_args = json.loads((self.repo / 'ui-args.json').read_text())
            self.assertEqual(ui_args[ui_args.index('--port') + 1], '18075')
            self.assertIn('--echo-console', ui_args)
            self.assertNotIn('--backplate', ui_args)
            self.assertTrue(ui_args[ui_args.index('--board') + 1].endswith('/board.sock'))
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
            for path in (ui_pid, Path(str(self.capture) + '.pid')):
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(path.read_text()), 0)
            self.assertFalse(list((self.repo / '.qemu').glob('onyx-ui-*')))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

    def test_ui_failure_stops_qemu(self):
        (self.repo / 'ui/ui.py').write_text('import sys\nsys.exit(7)\n')
        result = self.run_sh('--onyx', '--ui', QEMU_WAIT='60')
        self.assertEqual(result.returncode, 7, result.stderr)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(Path(str(self.capture) + '.pid').read_text()), 0)

    def test_qemu_exit_stops_ui(self):
        pid = self.repo / 'ui.pid'
        (self.repo / 'ui/ui.py').write_text(
            'import os, pathlib, time\n'
            f'pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n'
            'time.sleep(60)\n')
        result = self.run_sh('--onyx', '--ui', QEMU_WAIT='0.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)

    def test_docker_console_command(self):
        args = argparse.Namespace(qemu='/qemu', nand='/nand', snapshot='off',
                                  extra=['--', '-S'], ui=False, docker='test-image',
                                  open_security=False, boot_rom=None, sdp_image=None,
                                  sdp_fit=None)
        with mock.patch.object(launcher.os, 'execvp') as execute:
            # exec never returns in production.
            execute.side_effect = SystemExit
            with self.assertRaises(SystemExit):
                launcher.launch(args)
        command = execute.call_args.args[1]
        self.assertEqual(command[:3], ['docker', 'run', '--rm'])
        self.assertIn('test-image', command)
        self.assertEqual(command[-1], '-S')


if __name__ == '__main__':
    unittest.main()
