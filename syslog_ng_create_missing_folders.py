#!/usr/bin/python3


# pylint: disable=line-too-long


"""
Parse syslog-ng config files and create missing paths
"""


import os
import re
import logging
import pathlib
from typing import List


LOGGER = logging.getLogger("MissingSyslogNgFolder")


def find_config_files(folder: pathlib.Path) -> List[pathlib.Path]:
    """
    Walk into given directory and return all filesname being inside

    :param folder: Folder to walk into
    :type folder: pathlib.Path
    :return: List of files being inside directory (files only, recursive) as pathlib.Path instances
    :rtype: list
    """

    assert isinstance(folder, pathlib.Path), "folder parameter must be a pathlib.Path instance"
    assert folder.is_dir(), "folder parameter must be an existing directory"
    assert os.access(str(folder), os.R_OK), "folder parameter must have read access"
    assert os.access(str(folder), os.X_OK), "folder parameter must have execution access"

    files = folder.glob("**/*")
    return [x for x in files if os.access(str(x), os.R_OK) and x.is_file() and not x.is_symlink()]


def find_path_in_config(config: pathlib.Path) -> List[pathlib.Path]:
    """
    Load config file content and extract file path from file("") and dir("") directives

    Pretty shitty regex based parsing but seems to work pretty well and pyparsing learning curve
    seemed a bit hard to me

    :param config: Path to config file
    :type config: pathlib.Path
    :return: Folder used in this config as pathlib.Path instances
    :rtype: list
    """

    with open(config, "rb") as config_fh:
        config_str = config_fh.read()
        file_paths = re.findall(rb'file\(\s*"(.+?)"', config_str)
        dir_paths = re.findall(rb'dir\(\s*"(.+?)"', config_str)
    folders = [pathlib.Path(str(x, "utf-8")).parent for x in file_paths] + [pathlib.Path(str(x, "utf-8")) for x in dir_paths]
    # Remove files with $ placeholder
    folders = [folder for folder in folders if not any(part.startswith("$") for part in folder.parts)]
    return folders


def find_all_paths(folder: pathlib.Path) -> List[pathlib.Path]:
    """
    Find all path used in file("") and dir("") directives for all config files being in provided folder

    :param folder: Folder to walk into
    :type folder: pathlib.Path
    :return: List of path used in all config files, duplicates and parents removed
    :rtype: list
    """

    config_files = find_config_files(folder)
    config_dirs: List[pathlib.Path] = []
    for config_file in config_files:
        config_dirs += find_path_in_config(config_file)

    # Remove dups
    config_dirs = list(set(config_dirs))
    # Remove parents if nested child also here
    config_dirs = [x for x in config_dirs if not any(x in y.parents for y in config_dirs)]
    return config_dirs


if __name__ == "__main__":

    import sys
    import shutil
    import argparse

    def cli_arguments() -> argparse.Namespace:
        """
        Parse argument from command line and return Namespace object

        :return: Namespace object containing all properties (dash replaced by underscore)
        :rtype: argparse.Namespace
        """

        os.environ["COLUMNS"] = str(shutil.get_terminal_size().columns)

        parser = argparse.ArgumentParser(description=__doc__.strip(), formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        parser.add_argument("--syslog-ng-conf-dir", type=str, required=True, help="Folder containing syslog-ng configuration", metavar="/etc/syslog-ng")
        parsed = parser.parse_args()
        return parsed

    def main() -> None:
        """
        Start application
        """

        if os.getenv("NO_LOGS_TS", None) is not None:
            log_formatter = "%(levelname)-8s [%(name)s] %(message)s"
        else:
            log_formatter = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        logging.basicConfig(level=logging.INFO, format=log_formatter, stream=sys.stdout)

        config = cli_arguments()

        paths = find_all_paths(pathlib.Path(config.syslog_ng_conf_dir))
        for path in paths:
            if path.exists():
                logging.debug("Folder %s already exists", path)
            else:
                logging.info("Creating missing folder %s", path)
                path.mkdir(parents=True, exist_ok=True)

    try:
        main()
    except KeyboardInterrupt:
        logging.info("Exiting on SIGINT")
