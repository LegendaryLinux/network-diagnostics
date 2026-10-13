"""Private per-endpoint storage for derived authentication keys, never passphrases."""

import hashlib
import os
from pathlib import Path
import secrets
import stat


VERSION = b'netdiag-key-v1\0'


def _directory(create):
    root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'network-diagnostics'
    if not root.is_absolute():
        raise ValueError('Credential storage requires an absolute XDG_CONFIG_HOME path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open('/', flags)
    try:
        for index, part in enumerate(root.parts[1:], 1):
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    os.close(directory)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            if index == len(root.parts)-2 and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
                raise ValueError('Credential configuration directory must be owned by your user and not writable by others')
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('Credential directory must be owned by your user with mode 0700')
        return directory
    except BaseException:
        os.close(directory)
        raise


def _filename(remote):
    return hashlib.sha256(f"{remote['host'].lower().rstrip('.')}:{remote['port']}".encode('ascii')).hexdigest()+'.key'


def _read(directory, name):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('Remembered key must be an owned, single-link regular file with mode 0600')
        return stream.read(len(VERSION)+33)


def load_saved_key(remote):
    directory = None
    try:
        directory = _directory(False)
        if directory is None:
            return None
        data = _read(directory, _filename(remote))
        if data is None:
            return None
        if len(data) != len(VERSION)+32 or not data.startswith(VERSION):
            raise ValueError('Remembered key has an invalid format; use --ask-passphrase to replace it')
        return data[len(VERSION):]
    except OSError:
        raise ValueError('Cannot safely read remembered authentication key') from None
    finally:
        if directory is not None:
            os.close(directory)


def save_key(remote, key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError('Only a 32-byte derived authentication key can be remembered')
    directory = None
    temporary = secrets.token_hex(16)+'.tmp'
    try:
        directory = _directory(True)
        name = _filename(remote)
        _read(directory, name)  # Refuse unsafe existing files, even when replacing a key.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(VERSION+key)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except OSError:
        raise ValueError('Cannot safely save authentication key; collection was not started') from None
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)
