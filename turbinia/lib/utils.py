# -*- coding: utf-8 -*-
# Copyright 2018 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Common utils."""

from __future__ import unicode_literals

import logging
import os
import subprocess
import tempfile
import threading

from turbinia import config
from turbinia import TurbiniaException

log = logging.getLogger('turbinia')

DEFAULT_TIMEOUT = 7200


def _image_export(command, output_dir, disk_path, timeout=DEFAULT_TIMEOUT):
  """Runs image_export command.

  Args:
    command: image_export command to run.
    output_dir: Path to directory to store the extracted files.
    disk_path: Path to either a raw disk image or a block device.

  Returns:
    list: paths to extracted files.

  Raises:
    TurbiniaException: If an error occurs when running image_export.
  """

  # Execute the job via docker if docker is enabled and the Job has an image configured
  # The image should be of the log2timeline/plaso type.
  config.LoadConfig()
  dependencies = config.ParseDependencies()
  docker_image = None
  job_name = 'FileArtifactExtractionJob'.lower()
  if dependencies.get(job_name):
    docker_image = dependencies.get(job_name).get('docker_image')
  if (config.DOCKER_ENABLED and docker_image is not None):
    from turbinia.lib import docker_manager
    ro_paths = [disk_path]
    rw_paths = [output_dir]
    container_manager = docker_manager.ContainerManager(docker_image)
    log.info(
        'Executing job {0:s} in container: {1:s}'.format(command, docker_image))
    job_timeout = dependencies.get(job_name).get('timeout')
    if job_timeout is None:
      job_timeout = timeout
    stdout, stderr, ret = container_manager.execute_container(
        command, shell=False, ro_paths=ro_paths, rw_paths=rw_paths,
        timeout_limit=job_timeout)
  else:  # execute with local install of image_export.py
    command.insert(0, 'sudo')
    log.debug(f"Running image_export as [{' '.join(command):s}]")
    try:
      subprocess.check_call(command, timeout=timeout)
    except subprocess.CalledProcessError as exception:
      raise TurbiniaException(f'image_export.py failed: {exception!s}')
    except subprocess.TimeoutExpired as exception:
      raise TurbiniaException(
          f'image_export.py timed out after {timeout:d}s: {exception!s}')

  collected_file_paths = []
  file_count = 0
  for dirpath, _, filenames in os.walk(output_dir):
    for filename in filenames:
      collected_file_paths.append(os.path.join(dirpath, filename))
      file_count += 1

  log.debug(f'Collected {file_count:d} files with image_export')
  return collected_file_paths


def extract_artifacts(artifact_names, disk_path, output_dir, credentials=[]):
  """Extract artifacts using image_export from Plaso.

  Args:
    artifact_names: List of artifact definition names.
    disk_path: Path to either a raw disk image or a block device.
    output_dir: Path to directory to store the extracted files.
    credentials: List of credentials to use for decryption.

  Returns:
    list: paths to extracted files.

  Raises:
    TurbiniaException: If an error occurs when running image_export.
  """
  # Plaso image_export expects artifact names as a comma separated string.
  artifacts = ','.join(artifact_names)
  image_export_cmd = [
      'image_export.py', '--artifact_filters', artifacts, '--write', output_dir,
      '--partitions', 'all', '--volumes', 'all', '--unattended'
  ]

  if credentials:
    for credential_type, credential_data in credentials:
      image_export_cmd.extend(
          ['--credential', f'{credential_type:s}:{credential_data:s}'])

  image_export_cmd.append(disk_path)

  return _image_export(image_export_cmd, output_dir, disk_path)


def extract_files(file_name, disk_path, output_dir, credentials=[]):
  """Extract files using image_export from Plaso.

  Args:
    file_name: Name of file (without path) to be extracted.
    disk_path: Path to either a raw disk image or a block device.
    output_dir: Path to directory to store the extracted files.
    credentials: List of credentials to use for decryption.

  Returns:
    list: paths to extracted files.

  Raises:
    TurbiniaException: If an error occurs when running image_export.
  """
  if not disk_path:
    raise TurbiniaException(
        'image_export.py failed: Attempted to run with no local_path')

  image_export_cmd = [
      'image_export.py', '--name', file_name, '--write', output_dir,
      '--partitions', 'all', '--volumes', 'all'
  ]

  if credentials:
    for credential_type, credential_data in credentials:
      image_export_cmd.extend(
          ['--credential', f'{credential_type:s}:{credential_data:s}'])

  image_export_cmd.append(disk_path)

  return _image_export(image_export_cmd, output_dir, disk_path)


def get_exe_path(filename):
  """Gets the full path for a given executable.

  Args:
    filename (str): Executable name.

  Returns:
    (str|None): Full file path if it exists, else None
  """
  binary = None
  for path in os.environ['PATH'].split(os.pathsep):
    tentative_path = os.path.join(path, filename)
    if os.path.exists(tentative_path):
      binary = tentative_path
      break

  return binary


def bruteforce_password_hashes(
    password_hashes, tmp_dir, timeout=300, extra_args=''):
  """Bruteforce password hashes using Hashcat or john.

  Args:
    password_hashes (list): Password hashes as strings.
    tmp_dir (str): Path to use as a temporary directory
    timeout (int): Number of seconds to run for before terminating the process.
    extra_args (str): Any extra arguments to be passed to Hashcat.

  Returns:
    list: of tuples with hashes and plain text passwords.

  Raises:
    TurbiniaException if execution failed.
  """

  with tempfile.NamedTemporaryFile(delete=False, mode='w+') as fh:
    password_hashes_file_path = fh.name
    fh.write('\n'.join(password_hashes))

  pot_file = os.path.join((tmp_dir or tempfile.gettempdir()), 'hashcat.pot')
  password_list_file_path = os.path.expanduser('~/password.lst')
  password_rules_file_path = os.path.expanduser(
      '~/turbinia-password-cracking.rules')

  # Fallback
  if not os.path.isfile(password_list_file_path):
    password_list_file_path = '/usr/share/john/password.lst'

  # Bail
  if not os.path.isfile(password_list_file_path):
    raise TurbiniaException('No password list available')

  # Does rules file exist? If not make a temp one
  if not os.path.isfile(password_rules_file_path):
    with tempfile.NamedTemporaryFile(delete=False, mode='w+') as rf:
      password_rules_file_path = rf.name
      rf.write('\n'.join([':', 'd']))

  if '$y$' in ''.join(password_hashes):
    cmd = [
        'john', '--format=crypt', f'--wordlist={password_list_file_path}',
        password_hashes_file_path
    ]
    pot_file = os.path.expanduser('~/.john/john.pot')
  else:
    # Ignore warnings & plain word list attack (with rules)
    cmd = ['hashcat', '--force', '-a', '0']
    if extra_args:
      cmd = cmd + extra_args.split(' ')
    cmd = cmd + [f'--potfile-path={pot_file}']
    cmd = cmd + [password_hashes_file_path, password_list_file_path]
    cmd = cmd + ['-r', password_rules_file_path]

  with open(os.devnull, 'w') as devnull:
    try:
      child = subprocess.Popen(cmd, stdout=devnull, stderr=devnull)
      timer = threading.Timer(timeout, child.terminate)
      timer.start()
      child.communicate()
      # Cancel the timer if the process is done before the timer.
      if timer.is_alive():
        timer.cancel()
    except OSError as exception:
      raise TurbiniaException(f'{" ".join(cmd)} failed: {exception}')

  result = []

  if os.path.isfile(pot_file):
    with open(pot_file, 'r') as fh:
      for line in fh:
        password_hash, plaintext = line.rsplit(':', 1)
        plaintext = plaintext.rstrip()
        if plaintext:
          result.append((password_hash, plaintext))
    os.remove(pot_file)

  return result
