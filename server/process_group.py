"""进程组身份校验与信号投递原语 —— 无状态的一层，不知道 task / WS / 超时预算。

这一层的契约（18 轮 review 攒出来的，改动前先读完）：

* **所有信号按 ``(pgid, leader_start)`` 这个身份对投递，而不是按裸 pgid。** pid 只在
  进程活着的期间是个稳定 handle；观测和投递之间内核随时可以把这个号出让给别人，按裸
  pid 投递的那一刻起，孤儿清理就变成了误伤陌生人。
* **``killpg_verified`` 是唯一的投递闸门。** 检查一次然后后面接着发是不够的：SIGTERM、
  等待后的升级、shutdown drain 的每一次轮询都是独立的一发，两发之间号码就可能被重新
  出让，所以每一发都要现场重核身份。
* **四态语义（``group_state``）里 UNKNOWN 既不可投递、也不是死亡证据。** "别发信号"和
  "它已经没了"是两个不同的事实，混为一谈就是：一个扛过 SIGTERM 的后代还活着，而唯一
  能找到它的恢复元数据被当成"已消失"丢掉了。同理"读取失败 ≠ 观测结果" —— stat 读不出
  来只说明我们看不见，不说明进程不在。

与 task 状态、WS 广播、超时预算耦合的编排（``kill_task`` 的升级循环、``shutdown`` 的
三阶段预算、``restore_orphan_tasks``）留在 ``server/agent_runner.py``，只调用这里。
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def stat_fields_after_comm(stat_text: str) -> list[str]:
    """/proc/<pid>/stat 里 comm 之后的字段（state 起算，即原第 3 字段起）。

    comm 是括号包起来的进程名，本身可以含空格和括号，所以整行 split() 会让它后面
    所有字段整体错位 —— 取到的"start time"其实是别的字段（进程名带一个空格时通常
    读成 0）。这种被污染的身份还会撞车：另一个同样带空格名字的进程复用了 pid/pgid
    后算出同一个假值，group_state 就会把陌生人的组认成我们的并放行 killpg。
    唯一可靠的切法是从最后一个 ')' 之后开始。没有 ')' 说明这行不是合法 stat，返回
    空列表，让调用方按"读取失败"处理而不是按错位字段下判决。
    """
    _, sep, tail = stat_text.rpartition(")")
    if not sep:
        return []
    return tail.split()


def read_proc_start_time(pid: int) -> int | None:
    """Read /proc/<pid>/stat field 22 (process start time since boot)."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_fields_after_comm(stat_text)
        if len(fields) < 20:
            return None
        return int(fields[19])
    except Exception:
        return None


def read_ppid(pid: int) -> int | None:
    """Read /proc/<pid>/stat field 4 (parent pid)."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_fields_after_comm(stat_text)
        if len(fields) < 2:
            return None
        return int(fields[1])
    except Exception:
        return None


def recapture_leader_start(pid: int, unreaped: bool) -> int | None:
    """spawn 时身份读失败后补读一次，仅限 *pid* 仍是我们没回收的子进程时。

    身份闸门存在的前提是"号码可能已经被出让给别人"，而一个还没被 waitpid 收割的子
    进程会以 zombie 形式一直占着这个号 —— 内核不会重新分配它。所以在 *unreaped* 为真
    的窗口里补读到的 starttime 仍然是我们那个 leader 的，可以当基准用。spawn 那一次
    读失败（EIO/ENOMEM 之类的瞬时失败）不该让这一整组从此过不了闸门：PTY 路径没有
    proc 句柄可以兜底，那等于 `run.sh stop/restart` 永久漏掉这个组。

    额外核一次 ppid == 我们自己：万一 unreaped 的判断本身滞后（收割线程刚从 waitpid
    返回、标记还没写回），此时号码若已被重新出让，读到的身份不会被当成我们的放行。
    """
    if not unreaped:
        return None
    if read_ppid(pid) != os.getpid():
        return None
    return read_proc_start_time(pid)


def run_child_unreaped(run: dict) -> bool:
    """*run* 的直接子进程是否还没被我们 waitpid 回收。

    未回收的子进程即使已经退出也以 zombie 形式占着 pid，内核不会把这个号重新出让给
    别人 —— 这是"补读身份/按 pid 兜底不会打到复用者"的唯一依据。pipe 用 returncode
    （asyncio 收割之后才置值），PTY 用收割线程那个 future。两者都取不到就当"不能确定"
    处理，不补读也不兜底。
    """
    proc = run.get("proc")
    if proc is not None:
        return proc.returncode is None
    reaped = run.get("reaped")
    if reaped is not None:
        return not reaped.done()
    return False


def kill_direct_child(pid: int, sig: int) -> bool:
    """向仍是我们自家子进程的 *pid* 投递 *sig*；否则不发。

    PTY 路径在闸门拒发时的兜底：它没有 pipe 那样的 proc 句柄，killpg 一旦被拒
    （身份读不出来，或号码已被出让），直接子进程就一个信号都收不到，仅关掉
    transport 只是让读循环收尾，进程组照样活着。

    投递前现读一次 ppid：收割线程可能刚从 waitpid 返回而 future 标记还没写回，那个
    窗口里号码已经可以被重新出让。父进程是自己就排除了误伤陌生进程 —— 与
    killpg_verified 同样是"按身份而非裸 pid 投递"。
    """
    if read_ppid(pid) != os.getpid():
        return False
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def pid_is_absent(pid: int) -> bool:
    """True only when /proc has no entry for *pid* at all.

    Distinct from `read_proc_start_time(pid) is None`, which also covers a
    transient read/parse failure on a pid that very much still exists. Callers
    that signal a process group derived from *pid* must use this: treating an
    unreadable stat as proof of absence would let them kill a live, recycled
    stranger's group. ENOENT on the directory itself is the only safe evidence.
    """
    try:
        os.stat(f"/proc/{pid}")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def scan_pgroup(pgid: int) -> tuple[list[tuple[int, int | None]], bool]:
    """(members, complete) for process group *pgid*.

    `complete` is False when the scan could not see all of /proc — the directory
    listing failed, or a candidate's stat was unreadable for a reason other than
    the process having exited. An empty member list is then "we don't know",
    not "the group is empty": collapsing the two lets a transient read failure
    look like proof of death, which is how callers stop escalating or discard the
    only handle on a live writer-lock holder.
    """
    members: list[tuple[int, int | None]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members, False
    complete = True
    for name in entries:
        if not name.isdigit():
            continue
        try:
            stat_text = Path(f"/proc/{name}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            # Exited between listdir and read — a real observation, not a gap.
            continue
        except (OSError, ValueError):
            complete = False
            continue
        # state, ppid, pgrp, ..., starttime follow the parenthesised comm, which
        # can itself contain spaces and parens — split on the last ')'.
        fields = stat_fields_after_comm(stat_text)
        if len(fields) < 20:
            complete = False
            continue
        if fields[0] in ("Z", "X", "x"):
            continue
        try:
            if int(fields[2]) == pgid:
                members.append((int(name), int(fields[19])))
        except ValueError:
            complete = False
            continue
    return members, complete


def pgroup_member_ids(pgid: int) -> list[tuple[int, int | None]]:
    """Live (non-zombie) members of process group *pgid* as (pid, starttime).

    Returns identities, not bare pids: a pid is only a stable handle for as long
    as the process lives, and every caller here signals *after* observing. The
    start-time field pins which process a number referred to at enumeration
    time, so a number reused in between is detectable rather than silently
    inheriting a kill aimed at its predecessor.
    """
    return scan_pgroup(pgid)[0]


"""Verdicts from group_state. "Don't signal" and "it's gone" are different
facts, and conflating them is how a SIGTERM-resistant descendant survives while
its only recovery metadata is discarded."""
GROUP_OURS = "ours"          # ours and alive → safe to signal
GROUP_GONE = "gone"          # provably no live members → done with it
GROUP_FOREIGN = "foreign"    # pid re-leased → must not signal, not ours
GROUP_UNKNOWN = "unknown"    # cannot verify → must not signal, may still live


def group_state(pgid: int, leader_start: int | None) -> str:
    """Classify process group *pgid* against the leader's recorded start time.

    A leader that exited leaves its group addressable by the same pgid, so the
    leader's absence is not disqualifying — that orphaned-descendant shape is the
    whole point of this module's cleanup. But if some *other* live process now
    holds that pid, the number has been re-leased and signaling it would hit an
    unrelated tree.

    Returns GROUP_UNKNOWN rather than GROUP_GONE when identity cannot be
    established: callers must neither signal it nor treat it as disappeared.
    """
    current = read_proc_start_time(pgid)
    leader_absent = pid_is_absent(pgid)
    if current is not None and leader_start is not None and current != leader_start:
        return GROUP_FOREIGN
    if current is None and not leader_absent:
        # Unreadable is not absent: something holds this pid but we cannot tell
        # whether it is ours. Not signalable, and not evidence of death either.
        return GROUP_UNKNOWN
    if leader_start is None and not leader_absent:
        # No baseline to compare against while the pid is held.
        return GROUP_UNKNOWN
    members, complete = scan_pgroup(pgid)
    if not complete:
        # An incomplete /proc scan cannot prove the group is empty.
        return GROUP_UNKNOWN
    # 扫完再核一次 leader 身份。上面的检查只在那一瞬成立，而 scan_pgroup 要走一遍
    # /proc（几百毫秒量级）：这期间 leader 可能退出、pid 被回收并重新分配给一个新的
    # session leader，扫出来的成员就是它的组而不是我们的。之前只对"进来时就已缺席"
    # 的 leader 补检了重分配，活着的 leader 走不到那个分支 —— 判成 GROUP_OURS 后
    # killpg_verified 就会向陌生人的组投信号。
    after = read_proc_start_time(pgid)
    after_absent = pid_is_absent(pgid)
    if after is not None and leader_start is not None and after != leader_start:
        # 号码在扫描期间被重新出让给了别人。
        return GROUP_FOREIGN
    if leader_absent and not after_absent:
        # The number was free when we checked and is held now: it was re-leased
        # during enumeration, so a new session leader may own this pgid and the
        # members we just collected could be its, not ours. Absence is only ever
        # a point-in-time fact, which is why it is rechecked after the scan.
        return GROUP_FOREIGN
    if after is None and not after_absent:
        # 扫描后读不出身份：既不能确认成员属于我们，也不是死亡证据。
        return GROUP_UNKNOWN
    return GROUP_OURS if members else GROUP_GONE


def group_is_still(pgid: int, leader_start: int | None) -> bool:
    """True only when the group is verifiably ours and alive (safe to signal).

    Deliberately NOT a liveness test — see group_state. Callers asking "is it
    gone?" must compare against GROUP_GONE instead.
    """
    return group_state(pgid, leader_start) == GROUP_OURS


def terminate_group_blocking(pgid: int, leader_start: int | None) -> str:
    """SIGTERM → wait → SIGKILL → wait on group *pgid*; return the final verdict.

    Blocking on purpose: the only caller context is startup, before the event
    loop serves traffic, and the whole point is to not release the pgid until we
    know what happened to it. Every delivery re-verifies the leader identity via
    killpg_verified, and only a definite verdict (GROUP_GONE / GROUP_FOREIGN)
    ends a wait early — GROUP_UNKNOWN may still resolve on a later read.
    """
    if killpg_verified(pgid, leader_start, signal.SIGTERM):
        logger.info("Sent SIGTERM to orphan group pgid=%d", pgid)
    for _ in range(10):
        if group_state(pgid, leader_start) in (GROUP_GONE, GROUP_FOREIGN):
            break
        time.sleep(0.1)
    if group_is_still(pgid, leader_start):
        logger.warning("Orphan group pgid=%d survived SIGTERM; escalating to SIGKILL", pgid)
        killpg_verified(pgid, leader_start, signal.SIGKILL)
        # SIGKILL is not synchronous either: a member wedged in an
        # uninterruptible wait keeps it pending and stays alive. Confirm before
        # any caller releases the metadata — this pgid is the only handle on the
        # lock holder, so dropping it while the group lives trades a recoverable
        # orphan for a permanent one.
        for _ in range(10):
            if group_state(pgid, leader_start) in (GROUP_GONE, GROUP_FOREIGN):
                break
            time.sleep(0.1)
    return group_state(pgid, leader_start)


def killpg_verified(pgid: int, leader_start: int | None, sig: int) -> bool:
    """Signal process group *pgid*, but only while it is still ours.

    Every killpg in this module goes through here. Checking once and then
    signaling later is not enough: each of SIGTERM, the post-wait escalation and
    each poll of shutdown's drain is a separate delivery, and the pgid can be
    re-leased between any two of them. Returns whether the signal was sent.
    """
    if not group_is_still(pgid, leader_start):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def kill_verified(member: int, start_time: int | None) -> None:
    """SIGTERM+SIGKILL *member*, but only while it is still the same process.

    Re-reads the start time immediately before each signal: between enumeration
    and delivery the kernel is free to hand this number to something unrelated,
    and killing by bare pid at that point is how orphan cleanup turns into
    collateral damage.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if read_proc_start_time(member) != start_time:
            return
        try:
            os.kill(member, sig)
        except (ProcessLookupError, PermissionError):
            return
        except Exception:
            return


def pgroup_members(pgid: int) -> list[int]:
    """Live (non-zombie) pids in process group *pgid*.

    kill(-pgid, 0) is not a substitute: it succeeds as long as the group still
    holds a zombie, and it says nothing about *which* processes remain — orphan
    recovery needs the members themselves once the group leader is gone.
    """
    return [pid for pid, _start in pgroup_member_ids(pgid)]


def pgroup_alive(pgid: int) -> bool:
    """True while any non-zombie process remains in process group *pgid*.

    The direct child's returncode is not a substitute: a wrapper that exits
    promptly on SIGTERM says nothing about a descendant that ignored it, and
    that descendant is exactly the writer-lock holder we need gone.
    """
    return bool(pgroup_members(pgid))
