# -*- coding: utf-8 -*-
"""beacon.shellrun — run a shell command the way Claude Code runs a hook command.

POSIX: /bin/sh -c. Windows: Claude Code runs command hooks through Git Bash, so a command a human
writes for beacon (a checklist's verify_cmd, notify_cmd) and the hook commands beacon installs are
POSIX shell there too. Git Bash is found the way Claude Code finds it: CLAUDE_CODE_GIT_BASH_PATH, the
two Program Files locations, then <git.exe>\\..\\..\\bin\\bash.exe -- never a bare "bash", which
CreateProcess looks up in System32 first and so finds WSL's bash.exe stub. Without Git Bash, cmd.exe.
Output is decoded as UTF-8 (errors replaced), never with the Windows ANSI code page.
"""
import os
import shutil
import signal
import subprocess


def git_bash():
    """Git Bash's bash.exe on Windows, else None (and always None elsewhere)."""
    if os.name != "nt":
        return None
    cands = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "").strip(),
             r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    git = shutil.which("git")
    if git:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _kill_tree(proc):
    """Kill the command's whole process tree: it runs in a session / process group of its own, so a grandchild
    (a script's `sleep`, a hung tool it started) dies with it. The pattern of watchdog.CliRunner.stop."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)       # pgid == pid: start_new_session made it the group leader
        except OSError:
            pass
    try:
        proc.kill()                                   # the leader itself, if the group kill could not reach it
    except OSError:
        pass


def run(cmd, cwd=None, env=None, timeout=None, input=None, merge=False):
    """-> subprocess.CompletedProcess with str stdout/stderr. Raises what subprocess.run raises
    (subprocess.TimeoutExpired with the output so far, OSError when it cannot start).
    merge=True: stderr goes into stdout in the order it was written (stderr is then None).
    A timeout (or any interruption) kills the command's whole process tree, not just the shell: a verify
    script that hangs, or a notify_cmd, never leaves processes behind across a run."""
    kw = dict(cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
              stdin=subprocess.PIPE if input is not None else None,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    bash = git_bash()
    args = [bash, "-c", cmd] if bash else cmd
    with subprocess.Popen(args, shell=not bash, **kw) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)   # the tree is dead: its pipes close
            except subprocess.TimeoutExpired:            # something outside the tree holds the pipes
                out, err = (x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x in (e.output, e.stderr))
            raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err) from None
        except BaseException:
            _kill_tree(proc)
            raise
        rc = proc.poll()
    return subprocess.CompletedProcess(proc.args, rc, out, err)


def posix_path(path):
    """A path spelled for a command that Git Bash will parse: on Windows, C:\\x\\y becomes C:/x/y (bash takes
    backslashes as escapes, so a hook command with them silently never runs). Unchanged elsewhere."""
    return str(path).replace("\\", "/") if os.name == "nt" else str(path)


__all__ = ["git_bash", "run", "posix_path"]
