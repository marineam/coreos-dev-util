# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Module containing classes that wrap artifact downloads."""

import os
import re
import shutil
import subprocess

import gsutil_util
import log_util


# Names of artifacts we care about.
AUTOTEST_PACKAGE = 'autotest.tar'
AUTOTEST_ZIPPED_PACKAGE = 'autotest.tar.bz2'
DEBUG_SYMBOLS = 'debug.tgz'
FIRMWARE_ARCHIVE = 'firmware_from_source.tar.bz2'
IMAGE_ARCHIVE = 'image.zip'
ROOT_UPDATE = 'update.gz'
STATEFUL_UPDATE = 'stateful.tgz'
TEST_IMAGE = 'chromiumos_test_image.bin'
TEST_SUITES_PACKAGE = 'test_suites.tar.bz2'
AU_SUITE_PACKAGE = 'au_control.tar.bz2'


class ArtifactDownloadError(Exception):
  """Error used to signify an issue processing an artifact."""
  pass


class BuildArtifact(log_util.Loggable):
  """Wrapper around an artifact to download from gsutil.

  The purpose of this class is to download objects from Google Storage
  and install them to a local directory. There are two main functions, one to
  download/prepare the artifacts in to a temporary staging area and the second
  to stage it into its final destination.
  """
  def __init__(self, gs_path, tmp_staging_dir, install_path, synchronous=False):
    """Args:
      gs_path: Path to artifact in google storage.
      tmp_staging_dir: Temporary working directory maintained by caller.
      install_path: Final destination of artifact.
      synchronous: If True, artifact must be downloaded in the foreground.
    """
    super(BuildArtifact, self).__init__()
    self._gs_path = gs_path
    self._tmp_staging_dir = tmp_staging_dir
    self._tmp_stage_path = os.path.join(tmp_staging_dir,
                                        os.path.basename(self._gs_path))
    self._synchronous = synchronous
    self._install_path = install_path

    if not os.path.isdir(self._tmp_staging_dir):
      os.makedirs(self._tmp_staging_dir)

    if not os.path.isdir(os.path.dirname(self._install_path)):
      os.makedirs(os.path.dirname(self._install_path))

  def Download(self):
    """Stages the artifact from google storage to a local staging directory."""
    gsutil_util.DownloadFromGS(self._gs_path, self._tmp_stage_path)

  def Synchronous(self):
    """Returns False if this artifact can be downloaded in the background."""
    return self._synchronous

  def Stage(self):
    """Moves the artifact from the tmp staging directory to the final path."""
    shutil.move(self._tmp_stage_path, self._install_path)

  def __str__(self):
    """String representation for the download."""
    return '->'.join([self._gs_path, self._tmp_staging_dir, self._install_path])


class AUTestPayloadBuildArtifact(BuildArtifact):
  """Wrapper for AUTest delta payloads which need additional setup."""
  def Stage(self):
    super(AUTestPayloadBuildArtifact, self).Stage()

    payload_dir = os.path.dirname(self._install_path)
    # Setup necessary symlinks for updating.
    os.symlink(os.path.join(os.pardir, os.pardir, TEST_IMAGE),
               os.path.join(payload_dir, TEST_IMAGE))
    os.symlink(os.path.join(os.pardir, os.pardir, STATEFUL_UPDATE),
               os.path.join(payload_dir, STATEFUL_UPDATE))


class TarballBuildArtifact(BuildArtifact):
  """Wrapper around an artifact to download from gsutil which is a tarball."""

  def _ExtractTarball(self, exclude=None):
    """Detects whether the tarball is compressed or not based on the file
    extension and extracts the tarball into the install_path with optional
    exclude path."""

    exclude_str = '--exclude=%s' % exclude if exclude else ''
    tarball = os.path.basename(self._tmp_stage_path)

    if re.search('.tar.bz2$', tarball):
      compress_str = '--use-compress-prog=pbzip2'
    else:
      compress_str = ''

    cmd = 'tar xf %s %s %s --directory=%s' % (
        self._tmp_stage_path, exclude_str, compress_str, self._install_path)
    msg = 'An error occurred when attempting to untar %s' % self._tmp_stage_path

    try:
      subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError, e:
      raise ArtifactDownloadError('%s %s' % (msg, e))

  def Stage(self):
    """Changes directory into the install path and untars the tarball."""
    if not os.path.isdir(self._install_path):
      os.makedirs(self._install_path)

    self._ExtractTarball()


class AutotestTarballBuildArtifact(TarballBuildArtifact):
  """Wrapper around the autotest tarball to download from gsutil."""

  def Stage(self):
    """Untars the autotest tarball into the install path excluding test suites.
    """
    if not os.path.isdir(self._install_path):
      os.makedirs(self._install_path)

    self._ExtractTarball(exclude='autotest/test_suites')
    autotest_dir = os.path.join(self._install_path, 'autotest')
    autotest_pkgs_dir = os.path.join(autotest_dir, 'packages')
    if not os.path.exists(autotest_pkgs_dir):
      os.makedirs(autotest_pkgs_dir)

    if not os.path.exists(os.path.join(autotest_pkgs_dir, 'packages.checksum')):
      cmd = 'autotest/utils/packager.py upload --repository=%s --all' % (
          autotest_pkgs_dir)
      msg = 'Failed to create autotest packages!'
      try:
        subprocess.check_call(cmd, cwd=self._tmp_staging_dir,
                              shell=True)
      except subprocess.CalledProcessError, e:
        raise ArtifactDownloadError('%s %s' % (msg, e))
    else:
      self._Log('Using pre-generated packages from autotest')

    # TODO(scottz): Remove after we have moved away from the old test_scheduler
    # code.
    cmd = 'cp %s/* %s' % (autotest_pkgs_dir, autotest_dir)
    subprocess.check_call(cmd, shell=True)


class DebugTarballBuildArtifact(TarballBuildArtifact):
  """Wrapper around the debug symbols tarball to download from gsutil."""

  def _ExtractTarball(self, _exclude=None):
    """Extracts debug/breakpad from the tarball into the install_path."""
    cmd = 'tar xzf %s --directory=%s debug/breakpad' % (
        self._tmp_stage_path, self._install_path)
    msg = 'An error occurred when attempting to untar %s' % self._tmp_stage_path
    try:
      subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError, e:
      raise ArtifactDownloadError('%s %s' % (msg, e))


class ZipfileBuildArtifact(BuildArtifact):
  """A downloadable artifact that is a zipfile.

  This class defines an extra public method for setting the list of files to be
  extracted upon staging. Staging amounts to unzipping the desired files to the
  install path.

  """

  def __init__(self, gs_path, tmp_staging_dir, install_path, synchronous=False,
               unzip_file_list=None):
    super(ZipfileBuildArtifact, self).__init__(
        gs_path, tmp_staging_dir, install_path, synchronous)
    self._unzip_file_list = unzip_file_list

  def _Unzip(self):
    """Unzip files into the install path."""

    cmd = 'unzip -o %s -d %s%s' % (
        self._tmp_stage_path,
        os.path.join(self._install_path),
        (' ' + ' '.join(self._unzip_file_list)
         if self._unzip_file_list else ''))
    self._Log('unzip command: %s' % cmd)
    msg = 'An error occurred when attempting to unzip %s' % self._tmp_stage_path

    try:
      subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError, e:
      raise ArtifactDownloadError('%s %s' % (msg, e))

  def Stage(self):
    """Unzip files into the install path."""
    if not os.path.isdir(self._install_path):
      os.makedirs(self._install_path)

    self._Unzip()
