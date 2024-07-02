#
#  Copyright (C) 2016 Stavros Korokithakis
#  Copyright (C) 2017 Codethink Limited
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	 See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library. If not, see <http://www.gnu.org/licenses/>.
#
#  Authors:
#        Tristan Van Berkom <tristan.vanberkom@codethink.co.uk>
#
#  The filesystem operations implementation here is based
#  on some example code written by Stavros Korokithakis.

import errno
import os
import shutil
import stat
import tempfile

from .fuse import FuseOSError, Operations

from .mount import Mount


# SafeHardlinks()
#
# A FUSE mount which implements a copy on write hardlink experience.
#
# Args:
#     root (str): The underlying filesystem path to mirror
#     tmp (str): A directory on the same filesystem for creating temp files
#
class SafeHardlinks(Mount):

    def __init__(self, directory, tempdir):
        self.directory = directory
        self.tempdir = tempdir

    def create_operations(self):
        return SafeHardlinkOps(self.directory, self.tempdir)


# SafeHardlinkOps()
#
# The actual FUSE Operations implementation below.
#
class SafeHardlinkOps(Operations):

    def __init__(self, root, tmp):
        self.root = root
        self.tmp = tmp

    def _full_path(self, partial):
        if partial.startswith("/"):
            partial = partial[1:]
        path = os.path.join(self.root, partial)
        return path

    def _ensure_copy(self, full_path, follow_symlinks=True):
        try:
            if follow_symlinks:
                # Follow symbolic links manually here
                real_path = os.path.realpath(full_path)
            else:
                real_path = full_path

            file_stat = os.stat(real_path, follow_symlinks=False)

            # Skip the file if it's not a hardlink
            if file_stat.st_nlink <= 1:
                return

            # For some reason directories may have st_nlink > 1, but they
            # cannot be hardlinked, so just ignore those.
            #
            if not stat.S_ISDIR(file_stat.st_mode):
                with tempfile.TemporaryDirectory(dir=self.tmp) as tempdir:
                    basename = os.path.basename(real_path)
                    temp_path = os.path.join(tempdir, basename)

                    # First copy, then unlink origin and rename
                    shutil.copy2(real_path, temp_path, follow_symlinks=False)
                    os.unlink(real_path)
                    os.rename(temp_path, real_path)

        except FileNotFoundError:
            # This doesnt exist yet, assume we're about to create it
            # so it's not a problem.
            pass

    ###########################################################
    #                     Fuse Methods                        #
    ###########################################################
    def access(self, path, amode):
        full_path = self._full_path(path)
        if not os.access(full_path, amode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        full_path = self._full_path(path)

        # Ensure copies on chmod
        self._ensure_copy(full_path)
        return os.chmod(full_path, mode)

    def chown(self, path, uid, gid):
        full_path = self._full_path(path)

        # Ensure copies on chown
        self._ensure_copy(full_path, follow_symlinks=False)
        return os.chown(full_path, uid, gid, follow_symlinks=False)

    def getattr(self, path, fh=None):
        full_path = self._full_path(path)
        st = os.lstat(full_path)
        return dict((key, getattr(st, key)) for key in (
            'st_atime', 'st_ctime', 'st_gid', 'st_mode',
            'st_mtime', 'st_nlink', 'st_size', 'st_uid',
            'st_ino'))

    def readdir(self, path, fh):
        full_path = self._full_path(path)

        dir_entries = ['.', '..']
        if os.path.isdir(full_path):
            dir_entries.extend(os.listdir(full_path))
        for entry in dir_entries:
            entry_full_path = os.path.join(full_path, entry)
            st = os.stat(entry_full_path, follow_symlinks=False)

            attrs = dict((key, getattr(st, key)) for key in (
                'st_ino', 'st_mode'))

            yield entry, attrs, 0

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            # Path name is absolute, sanitize it.
            return os.path.relpath(pathname, self.root)
        else:
            return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        full_path = self._full_path(path)
        return os.rmdir(full_path)

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        full_path = self._full_path(path)
        stv = os.statvfs(full_path)
        return dict((key, getattr(stv, key)) for key in (
            'f_bavail', 'f_bfree', 'f_blocks', 'f_bsize', 'f_favail',
            'f_ffree', 'f_files', 'f_flag', 'f_frsize', 'f_namemax'))

    def unlink(self, path):
        return os.unlink(self._full_path(path))

    def symlink(self, target, source):
        return os.symlink(source, self._full_path(target))

    def rename(self, old, new):
        return os.rename(self._full_path(old), self._full_path(new))

    def link(self, target, source):

        # When creating a hard link here, should we ensure the original
        # file is not a hardlink itself first ?
        #
        return os.link(self._full_path(source), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    def open(self, path, flags):
        full_path = self._full_path(path)

        # If we're opening for writing, ensure it's a copy first
        if flags & os.O_WRONLY or flags & os.O_RDWR:
            self._ensure_copy(full_path)

        return os.open(full_path, flags)

    def create(self, path, mode, fi=None):
        full_path = self._full_path(path)

        # If it already exists, ensure it's a copy first
        self._ensure_copy(full_path)
        return os.open(full_path, fi, mode)

    def read(self, path, size, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def write(self, path, data, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.write(fh, data)

    def truncate(self, path, length, fh=None):
        full_path = self._full_path(path)
        with open(full_path, 'r+', encoding='utf-8') as f:
            f.truncate(length)

    def flush(self, path, fh):
        return os.fsync(fh)

    def release(self, path, fh):
        return os.close(fh)

    def fsync(self, path, datasync, fh):
        return self.flush(path, fh)
