"""Run as the image's own user, not root.

The container starts as root because the data folder is mounted from the host and may belong to anyone;
a ``USER`` line would leave a bot that cannot write its database after an upgrade. So the bot gives the
data folder to its user first, then drops root, before it reads anything from Discord or the web. If
that cannot be done (no such user, a read-only folder), it says so and keeps running as before."""
import logging
import os

log = logging.getLogger("aion2.privileges")


def drop(user: str, data_dir: str) -> bool:
    """Give ``data_dir`` to ``user`` and become that user. True when the process now runs as ``user``."""
    if os.name != "posix" or not user or os.geteuid() != 0:
        return False
    import pwd
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        log.warning("No user %r in this image; running as root", user)
        return False
    try:
        os.makedirs(data_dir, exist_ok=True)
        for folder, _dirs, files in os.walk(data_dir):
            os.chown(folder, entry.pw_uid, entry.pw_gid, follow_symlinks=False)
            for name in files:
                os.chown(os.path.join(folder, name), entry.pw_uid, entry.pw_gid, follow_symlinks=False)
        os.setgroups([])
        os.setgid(entry.pw_gid)
        os.setuid(entry.pw_uid)
    except OSError as err:
        log.warning("Could not hand %s to %s (%s); running as root", data_dir, user, err)
        return False
    os.environ["HOME"] = entry.pw_dir
    log.info("Running as %s (uid %d)", user, entry.pw_uid)
    return True
