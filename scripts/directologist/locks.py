"""Nonblocking process lock using native OS primitives; no external dependency."""
import errno
import os
from contextlib import contextmanager
from .contracts import ContractError

@contextmanager
def file_lock(path):
    if path.is_symlink():raise ContractError('Ссылка вместо файла блокировки запрещена.')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET)
            try:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            except OSError as exc:
                if exc.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):raise ContractError('Для проекта уже работает другой процесс.') from None
                raise
        else:
            import fcntl
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ContractError('Для проекта уже работает другой процесс.') from None
        acquired=True
        yield
    finally:
        if acquired and os.name=='nt':
            import msvcrt
            os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
        os.close(fd)
