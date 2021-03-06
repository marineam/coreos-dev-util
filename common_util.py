# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper class for interacting with the Dev Server."""

import base64
import binascii
import distutils.version
import errno
import hashlib
import os
import random
import re
import shutil
import time

import lockfile

import build_artifact
import gsutil_util
import log_util


# Module-local log function.
def _Log(message, *args):
  return log_util.LogWithTag('UTIL', message, *args)


AU_BASE = 'au'
NTON_DIR_SUFFIX = '_nton'
MTON_DIR_SUFFIX = '_mton'
UPLOADED_LIST = 'UPLOADED'
DEVSERVER_LOCK_FILE = 'devserver'

_HASH_BLOCK_SIZE = 8192


def CommaSeparatedList(value_list, is_quoted=False):
  """Concatenates a list of strings.

  This turns ['a', 'b', 'c'] into a single string 'a, b and c'. It optionally
  adds quotes (`a') around each element. Used for logging.

  """
  if is_quoted:
    value_list = ["`" + value + "'" for value in value_list]

  if len(value_list) > 1:
    return (', '.join(value_list[:-1]) + ' and ' + value_list[-1])
  elif value_list:
    return value_list[0]
  else:
    return ''

class CommonUtilError(Exception):
  """Exception classes used by this module."""
  pass


def ParsePayloadList(archive_url, payload_list):
  """Parse and return the full, delta, and firmware payload URLs.

  Args:
    archive_url: The URL of the Google Storage bucket.
    payload_list: A list filenames.

  Returns:
    Tuple of 4 payload URLs: (full, nton, mton, firmware).

  Raises:
    CommonUtilError: If full payload is missing or invalid.
  """
  full_payload_url = None
  mton_payload_url = None
  nton_payload_url = None
  firmware_payload_url = None

  for payload in payload_list:
    if '_full_' in payload:
      full_payload_url = '/'.join([archive_url, payload])
    elif '_delta_' in payload:
      # e.g. chromeos_{from_version}_{to_version}_x86-generic_delta_dev.bin
      from_version, to_version = payload.split('_')[1:3]
      if from_version == to_version:
        nton_payload_url = '/'.join([archive_url, payload])
      else:
        mton_payload_url = '/'.join([archive_url, payload])
    elif build_artifact.FIRMWARE_ARCHIVE in payload:
      firmware_payload_url = '/'.join([archive_url, payload])

  if not full_payload_url:
    raise CommonUtilError(
        'Full payload is missing or has unexpected name format.', payload_list)

  return (full_payload_url, nton_payload_url,
          mton_payload_url, firmware_payload_url)


def IsAvailable(pattern_list, uploaded_list):
  """Checks whether the target artifacts we wait for are available.

  This method searches the uploaded_list for a match for every pattern
  in the pattern_list. It aborts and returns false if no filename
  matches a given pattern.

  Args:
    pattern_list: List of regular expression patterns to identify
        the target artifacts.
    uploaded_list: List of all uploaded files.

  Returns:
    True if there is a match for every pattern; false otherwise.
  """

  # Pre-compile the regular expression patterns
  compiled_patterns = []
  for p in pattern_list:
    compiled_patterns.append(re.compile(p))

  for pattern in compiled_patterns:
    found = False
    for filename in uploaded_list:
      if re.search(pattern, filename):
        found = True
        break
    if not found:
      return False

  return True


def WaitUntilAvailable(to_wait_list, archive_url, err_str, timeout=600,
                       delay=10):
  """Waits until all target artifacts are available in Google Storage or
  until the request times out.

  This method polls Google Storage until all target artifacts are
  available or until the timeout occurs. Because we may not know the
  exact name of the target artifacts, the method accepts to_wait_list, a
  list of filename patterns, to identify whether an artifact whose name
  matches the pattern exists (e.g. use pattern '_full_' to search for
  the full payload 'chromeos_R17-1413.0.0-a1_x86-mario_full_dev.bin').

  Args:
    to_wait_list: List of regular expression patterns to identify
        the target artifacts.
    archive_url: URL of the Google Storage bucket.
    err_str: String to display in the error message.

  Returns:
    The list of artifacts in the Google Storage bucket.

  Raises:
    CommonUtilError: If timeout occurs.
  """

  cmd = 'gsutil cat %s/%s' % (archive_url, UPLOADED_LIST)
  msg = 'Failed to get a list of uploaded files.'

  deadline = time.time() + timeout
  while time.time() < deadline:
    uploaded_list = []
    to_delay = delay + random.uniform(.5 * delay, 1.5 * delay)
    try:
      # Run "gsutil cat" to retrieve the list.
      uploaded_list = gsutil_util.GSUtilRun(cmd, msg).splitlines()
    except gsutil_util.GSUtilError:
      # For backward compatibility, fallling back to use "gsutil ls"
      # when the manifest file is not present.
      cmd = 'gsutil ls %s/*' % archive_url
      msg = 'Failed to list payloads.'
      payload_list = gsutil_util.GSUtilRun(cmd, msg).splitlines()
      for payload in payload_list:
        uploaded_list.append(payload.rsplit('/', 1)[1])

    # Check if all target artifacts are available.
    if IsAvailable(to_wait_list, uploaded_list):
      return uploaded_list
    _Log('Retrying in %f seconds...%s' % (to_delay, err_str))
    time.sleep(to_delay)

  raise CommonUtilError('Missing %s for %s.' % (err_str, archive_url))


def GatherArtifactDownloads(main_staging_dir, archive_url, build_dir, build,
                            timeout=600, delay=10):
  """Generates artifacts that we mean to download and install for autotest.

  This method generates the list of artifacts we will need for autotest. These
  artifacts are instances of build_artifact.BuildArtifact.

  Note, these artifacts can be downloaded asynchronously iff
  !artifact.Synchronous().
  """

  # Wait up to 10 minutes for the full payload to be uploaded because we
  # do not know the exact name of the full payload.

  # We also wait for 'autotest.tar' because we do not know what type of
  # autotest tarballs (tar or tar.bz2) is available
  # (crosbug.com/32312). This dependency can be removed once all
  # branches move to the new 'tar' format.
  to_wait_list = ['_full_', build_artifact.AUTOTEST_PACKAGE]
  err_str = 'full payload or autotest tarball'
  uploaded_list = WaitUntilAvailable(to_wait_list, archive_url, err_str,
                                     timeout=timeout, delay=delay)

  # First we gather the urls/paths for the update payloads.
  full_url, nton_url, mton_url, fw_url = ParsePayloadList(
      archive_url, uploaded_list)

  full_payload = os.path.join(build_dir, build_artifact.ROOT_UPDATE)

  artifacts = []
  artifacts.append(build_artifact.BuildArtifact(
      full_url, main_staging_dir, full_payload, synchronous=True))

  if nton_url:
    nton_payload = os.path.join(build_dir, AU_BASE, build + NTON_DIR_SUFFIX,
                                build_artifact.ROOT_UPDATE)
    artifacts.append(build_artifact.AUTestPayloadBuildArtifact(
      nton_url, main_staging_dir, nton_payload))

  if mton_url:
    mton_payload = os.path.join(build_dir, AU_BASE, build + MTON_DIR_SUFFIX,
                                build_artifact.ROOT_UPDATE)
    artifacts.append(build_artifact.AUTestPayloadBuildArtifact(
        mton_url, main_staging_dir, mton_payload))

  if fw_url:
    artifacts.append(build_artifact.BuildArtifact(
        fw_url, main_staging_dir, build_dir))

  # Gather information about autotest tarballs. Use autotest.tar if available.
  if build_artifact.AUTOTEST_PACKAGE in uploaded_list:
    autotest_url = '%s/%s' % (archive_url, build_artifact.AUTOTEST_PACKAGE)
  else:
    # Use autotest.tar.bz for backward compatibility. This can be
    # removed once all branches start using "autotest.tar"
    autotest_url = '%s/%s' % (
        archive_url, build_artifact.AUTOTEST_ZIPPED_PACKAGE)

  # Next we gather the miscellaneous payloads.
  stateful_url = archive_url + '/' + build_artifact.STATEFUL_UPDATE
  test_suites_url = (archive_url + '/' + build_artifact.TEST_SUITES_PACKAGE)

  stateful_payload = os.path.join(build_dir, build_artifact.STATEFUL_UPDATE)

  artifacts.append(build_artifact.BuildArtifact(
      stateful_url, main_staging_dir, stateful_payload, synchronous=True))
  artifacts.append(build_artifact.AutotestTarballBuildArtifact(
      autotest_url, main_staging_dir, build_dir))
  artifacts.append(build_artifact.TarballBuildArtifact(
      test_suites_url, main_staging_dir, build_dir, synchronous=True))
  return artifacts


def GatherSymbolArtifactDownloads(temp_download_dir, archive_url, staging_dir,
                                  timeout=600, delay=10):
  """Generates debug symbol artifacts that we mean to download and stage.

  This method generates the list of artifacts we will need to
  symbolicate crash dumps that occur during autotest runs.  These
  artifacts are instances of build_artifact.BuildArtifact.

  This will poll google storage until the debug symbol artifact becomes
  available, or until the 10 minute timeout is up.

  @param temp_download_dir: the tempdir into which we're downloading artifacts
                            prior to staging them.
  @param archive_url: the google storage url of the bucket where the debug
                      symbols for the desired build are stored.
  @param staging_dir: the dir into which to stage the symbols

  @return an iterable of one DebugTarballBuildArtifact pointing to the right
          debug symbols.  This is an iterable so that it's similar to
          GatherArtifactDownloads.  Also, it's possible that someday we might
          have more than one.
  """

  artifact_name = build_artifact.DEBUG_SYMBOLS
  WaitUntilAvailable([artifact_name], archive_url, 'debug symbols',
                     timeout=timeout, delay=delay)
  artifact = build_artifact.DebugTarballBuildArtifact(
      archive_url + '/' + artifact_name,
      temp_download_dir,
      staging_dir)
  return [artifact]


def GatherImageArchiveArtifactDownloads(temp_download_dir, archive_url,
                                        staging_dir, image_file_list,
                                        timeout=600, delay=10):
  """Generates image archive artifact(s) for downloading / staging.

  Generates the list of artifacts that are used for extracting Chrome OS images
  from. Currently, it returns a single artifact, which is a zipfile configured
  to extract a given list of images. It first polls Google Storage unti lthe
  desired artifacts become available (or a timeout expires).

  Args:
    temp_download_dir: temporary directory, used for downloading artifacts
    archive_url:       URI to the bucket where the artifacts are stored
    staging_dir:       directory into which to stage the extracted files
    image_file_list:   list of image files to be extracted
  Returns:
    list of downloadable artifacts (of type ZipfileBuildArtifact), currently
    containing a single obejct
  """

  artifact_name = build_artifact.IMAGE_ARCHIVE
  WaitUntilAvailable([artifact_name], archive_url, 'image archive',
                     timeout=timeout, delay=delay)
  artifact = build_artifact.ZipfileBuildArtifact(
      archive_url + '/' + artifact_name,
      temp_download_dir, staging_dir,
      unzip_file_list=image_file_list)
  return [artifact]


def PrepareBuildDirectory(build_dir):
  """Preliminary staging of installation directory for build.

  Args:
    build_dir: Directory to install build components into.
  """
  if not os.path.isdir(build_dir):
    os.path.makedirs(build_dir)

  # Create blank chromiumos_test_image.bin. Otherwise the Dev Server will
  # try to rebuild it unnecessarily.
  test_image = os.path.join(build_dir, build_artifact.TEST_IMAGE)
  open(test_image, 'a').close()


def SafeSandboxAccess(static_dir, path):
  """Verify that the path is in static_dir.

  Args:
    static_dir: Directory where builds are served from.
    path: Path to verify.

  Returns:
    True if path is in static_dir, False otherwise
  """
  static_dir = os.path.realpath(static_dir)
  path = os.path.realpath(path)
  return (path.startswith(static_dir) and path != static_dir)


def AcquireLock(static_dir, tag, create_once=True):
  """Acquires a lock for a given tag.

  Creates a directory for the specified tag, and atomically creates a lock file
  in it. This tells other components the resource/task represented by the tag
  is unavailable.

  Args:
    static_dir:  Directory where builds are served from.
    tag:         Unique resource/task identifier. Use '/' for nested tags.
    create_once: Determines whether the directory must be freshly created; this
                 preserves previous semantics of the lock acquisition.

  Returns:
    Path to the created directory or None if creation failed.

  Raises:
    CommonUtilError: If lock can't be acquired.
  """
  build_dir = os.path.join(static_dir, tag)
  if not SafeSandboxAccess(static_dir, build_dir):
    raise CommonUtilError('Invalid tag "%s".' % tag)

  # Create the directory.
  is_created = False
  try:
    os.makedirs(build_dir)
    is_created = True
  except OSError, e:
    if e.errno == errno.EEXIST:
      if create_once:
        raise CommonUtilError(str(e))
    else:
      raise

  # Lock the directory.
  try:
    lock = lockfile.FileLock(os.path.join(build_dir, DEVSERVER_LOCK_FILE))
    lock.acquire(timeout=0)
  except lockfile.AlreadyLocked, e:
    raise CommonUtilError(str(e))
  except:
    # In any other case, remove the directory if we actually created it, so
    # that subsequent attempts won't fail to re-create it.
    if is_created:
      shutil.rmtree(build_dir)
    raise

  return build_dir


def ReleaseLock(static_dir, tag, destroy=False):
  """Releases the lock for a given tag.

  Optionally, removes the locked directory entirely.

  Args:
    static_dir: Directory where builds are served from.
    tag:        Unique resource/task identifier. Use '/' for nested tags.
    destroy:    Determines whether the locked directory should be removed
                entirely.

  Raises:
    CommonUtilError: If lock can't be released.
  """
  build_dir = os.path.join(static_dir, tag)
  if not SafeSandboxAccess(static_dir, build_dir):
    raise CommonUtilError('Invalid tag "%s".' % tag)

  lock = lockfile.FileLock(os.path.join(build_dir, DEVSERVER_LOCK_FILE))
  try:
    lock.break_lock()
    if destroy:
      shutil.rmtree(build_dir)
  except Exception, e:
    raise CommonUtilError(str(e))


def GetLatestBuildVersion(static_dir, target, milestone=None):
  """Retrieves the latest build version for a given board.

  Args:
    static_dir: Directory where builds are served from.
    target: The build target, typically a combination of the board and the
        type of build e.g. x86-mario-release.
    milestone: For latest build set to None, for builds only in a specific
        milestone set to a str of format Rxx (e.g. R16). Default: None.

  Returns:
    If latest found, a full build string is returned e.g. R17-1234.0.0-a1-b983.
    If no latest is found for some reason or another a '' string is returned.

  Raises:
    CommonUtilError: If for some reason the latest build cannot be
        deteremined, this could be due to the dir not existing or no builds
        being present after filtering on milestone.
  """
  target_path = os.path.join(static_dir, target)
  if not os.path.isdir(target_path):
    raise CommonUtilError('Cannot find path %s' % target_path)

  builds = [distutils.version.LooseVersion(build) for build in
            os.listdir(target_path)]

  if milestone and builds:
    # Check if milestone Rxx is in the string representation of the build.
    builds = filter(lambda x: milestone.upper() in str(x), builds)

  if not builds:
    raise CommonUtilError('Could not determine build for %s' % target)

  return str(max(builds))


def GetControlFile(static_dir, build, control_path):
  """Attempts to pull the requested control file from the Dev Server.

  Args:
    static_dir: Directory where builds are served from.
    build: Fully qualified build string; e.g. R17-1234.0.0-a1-b983.
    control_path: Path to control file on Dev Server relative to Autotest root.

  Raises:
    CommonUtilError: If lock can't be acquired.

  Returns:
    Content of the requested control file.
  """
  # Be forgiving if the user passes in the control_path with a leading /
  control_path = control_path.lstrip('/')
  control_path = os.path.join(static_dir, build, 'autotest',
                              control_path)
  if not SafeSandboxAccess(static_dir, control_path):
    raise CommonUtilError('Invalid control file "%s".' % control_path)

  if not os.path.exists(control_path):
    # TODO(scottz): Come up with some sort of error mechanism.
    # crosbug.com/25040
    return 'Unknown control path %s' % control_path

  with open(control_path, 'r') as control_file:
    return control_file.read()


def GetControlFileList(static_dir, build):
  """List all control|control. files in the specified board/build path.

  Args:
    static_dir: Directory where builds are served from.
    build: Fully qualified build string; e.g. R17-1234.0.0-a1-b983.

  Raises:
    CommonUtilError: If path is outside of sandbox.

  Returns:
    String of each file separated by a newline.
  """
  autotest_dir = os.path.join(static_dir, build, 'autotest/')
  if not SafeSandboxAccess(static_dir, autotest_dir):
    raise CommonUtilError('Autotest dir not in sandbox "%s".' % autotest_dir)

  control_files = set()
  if not os.path.exists(autotest_dir):
    # TODO(scottz): Come up with some sort of error mechanism.
    # crosbug.com/25040
    return 'Unknown build path %s' % autotest_dir

  for entry in os.walk(autotest_dir):
    dir_path, _, files = entry
    for file_entry in files:
      if file_entry.startswith('control.') or file_entry == 'control':
        control_files.add(os.path.join(dir_path,
                                       file_entry).replace(autotest_dir, ''))

  return '\n'.join(control_files)


def GetFileSize(file_path):
  """Returns the size in bytes of the file given."""
  return os.path.getsize(file_path)


# Hashlib is strange and doesn't actually define these in a sane way that
# pylint can find them. Disable checks for them.
# pylint: disable=E1101,W0106
def GetFileHashes(file_path, do_sha1=False, do_sha256=False, do_md5=False):
  """Computes and returns a list of requested hashes.

  Args:
    file_path: path to file to be hashed
    do_sha1:   whether or not to compute a SHA1 hash
    do_sha256: whether or not to compute a SHA256 hash
    do_md5:    whether or not to compute a MD5 hash
  Returns:
    A dictionary containing binary hash values, keyed by 'sha1', 'sha256' and
    'md5', respectively.
  """
  hashes = {}
  if (do_sha1 or do_sha256 or do_md5):
    # Initialize hashers.
    hasher_sha1 = hashlib.sha1() if do_sha1 else None
    hasher_sha256 = hashlib.sha256() if do_sha256 else None
    hasher_md5 = hashlib.md5() if do_md5 else None

    # Read blocks from file, update hashes.
    with open(file_path, 'rb') as fd:
      while True:
        block = fd.read(_HASH_BLOCK_SIZE)
        if not block:
          break
        hasher_sha1 and hasher_sha1.update(block)
        hasher_sha256 and hasher_sha256.update(block)
        hasher_md5 and hasher_md5.update(block)

    # Update return values.
    if hasher_sha1:
      hashes['sha1'] = hasher_sha1.digest()
    if hasher_sha256:
      hashes['sha256'] = hasher_sha256.digest()
    if hasher_md5:
      hashes['md5'] = hasher_md5.digest()

  return hashes


def GetFileSha1(file_path):
  """Returns the SHA1 checksum of the file given (base64 encoded)."""
  return base64.b64encode(GetFileHashes(file_path, do_sha1=True)['sha1'])


def GetFileSha256(file_path):
  """Returns the SHA256 checksum of the file given (base64 encoded)."""
  return base64.b64encode(GetFileHashes(file_path, do_sha256=True)['sha256'])


def GetFileMd5(file_path):
  """Returns the MD5 checksum of the file given (hex encoded)."""
  return binascii.hexlify(GetFileHashes(file_path, do_md5=True)['md5'])


def CopyFile(source, dest):
  """Copies a file from |source| to |dest|."""
  _Log('Copy File %s -> %s' % (source, dest))
  shutil.copy(source, dest)
