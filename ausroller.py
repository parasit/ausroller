#!/usr/bin/env python2
# encoding: utf-8

import argparse
import sys
from jinja2 import Template, Environment, FileSystemLoader, exceptions
from gbp.git import repository
import subprocess
import ConfigParser
import os
import json
import shlex
import logging


RESOURCES = ["configmap", "deployment",
             "service", "pod", "replicationcontroller"]


class Ausroller(object):

    def __init__(self):
        # read cli parameters
        self.parse_args()

        # make log level configurable
        logging.basicConfig(format='%(levelname)s:\t%(message)s', level=logging.DEBUG)

        home_dir = os.path.expanduser("~")
        # read config file
        if not self.configfile:
            self.configfile = os.path.join(home_dir, ".ausroller.ini")
        self.read_config()
        # set paths and read in the json file with the secrets
        self.templates_path = os.path.join(self.repopath, 'templates')
        self.rollout_path = os.path.join(self.repopath, 'rollout', self.tenant)
        self.variablesfile = os.path.join(home_dir, ".ausroller_secrets.json")
        self.read_variables()
        self.kubectl_cmd = 'kubectl --namespace={}'.format(self.tenant)

    def parse_args(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('-a', '--app', type=str, required=True,
                            help='Application to rollout')
        parser.add_argument('-v', '--version', type=str, required=True,
                            help='Version to rollout')
        parser.add_argument('-m', '--message', type=str, required=False,
                            default='', help='Optional commit message')
        parser.add_argument('-c', '--config', type=str, required=False,
                            default='',
                            help='Path to config file [$HOME/.ausroller.ini]')
        parser.add_argument('-d', '--dryrun', action='store_true',
                            help='Don\'t do anything just print')
        parser.add_argument('-t', '--tenant', type=str, required=True,
                            help='Which tenant to rollout on')
        args = parser.parse_args()

        self.app_name = args.app
        self.app_version = args.version
        self.tenant = args.tenant
        self.commit_message = args.message
        self.is_dryrun = args.dryrun
        self.configfile = args.config

    def read_config(self):
        cp = ConfigParser.ConfigParser()
        try:
            cp.read(self.configfile)
        except:
            logging.error(
                "Cannot read configuration file \"{}\"!".format(self.configfile))
            sys.exit(1)

        try:
            self.repopath = cp.get('ausroller', 'repopath')
        except:
            logging.error("Cannot read 'repopath' from configuration file \"{}\"!".format(
                self.configfile))
            sys.exit(1)

    def read_variables(self):
        self.variables = {}

        try:
            with open(self.variablesfile) as f:
                self.variables = json.load(f)
        except:
            logging.error("Cannot read variables from \"{}\"!".format(self.variablesfile))
            sys.exit(1)

    def render_template(self, resource):
        '''
            app_name and resource type
        '''
        env = Environment(
            loader=FileSystemLoader(os.path.join(self.templates_path, resource + 's')))
        try:
            template = env.get_template(
                "{}-{}.tpl.yaml".format(self.app_name, resource))
        except exceptions.TemplateNotFound as e:
            logging.debug("Template \"{}\" not found.".format(e))
            return
        return template.render(self.variables, app_version=self.app_version, namespace=self.tenant)

    def prepare_rollout(self):
        logging.info("Preparing rollout of {} in version {}".format(
            self.app_name, self.app_version))
        result_map = {}
        for resource in RESOURCES:
            rendered_template = self.render_template(resource)
            if rendered_template:
                result_map[resource] = self.render_template(resource)
        return self.write_yamls(result_map)

    def write_yamls(self, resources):
        repo = repository.GitRepository(self.repopath)
        (repo_is_clean, repo_msg) = repo.is_clean()
        if not repo_is_clean:
            logging.error("Git repo is not in a clean state! Exiting..")
            sys.exit(1)

        files_to_commit = []
        for resource in resources.keys():
            # make sure path exists
            outdir = os.path.join(self.rollout_path, "{}s".format(resource))
            if not os.path.exists(outdir):
                try:
                    os.makedirs(outdir)
                except OSError:
                    # this is still not completely safe as we could run into a (next) race-condition, but it is suitable for out needs
                    if not os.path.exists(outdir):
                        logging.error("Can not create rollout directory for resource \"{}\"".format(resource))

            outfile = os.path.join(outdir, "{}-{}.yaml".format(self.app_name, resource))
            with open(outfile, 'w') as out:
                out.write(resources[resource])
                # flush & sync to avoid git adding an empty file
                out.flush()
                os.fsync(out)
                repo.add_files(outfile)
                files_to_commit.append(outfile)
        self.commit_rollout(files_to_commit)
        return resources.keys()

    def commit_rollout(self, files_to_commit):
        repo = repository.GitRepository(self.repopath)
        (repo_is_clean, repo_msg) = repo.is_clean()

        if not repo_is_clean:
            if self.is_dryrun:
                logging.debug("Dry run: skipping commit")
                return

            repo.commit_files(files_to_commit,
                              "Created rollout for {} with version {}\n\n{}".format(
                                  self.app_name,
                                   self.app_version,
                                   self.commit_message))
            logging.info(repo.show(self.rollout_path))
        else:
            logging.warn(
                "Definition of rollout already exists. Nothing changed.")

    def rollout(self, resources):
        logging.info("Rolling out resources {}".format(resources))
        for resource in resources:
            if self.is_dryrun:
                logging.info("Dry-run: skip applying changes to Kubernetes")
                return

            cmd = shlex.split("{} apply -f {}".format(self.kubectl_cmd, os.path.join(
                self.rollout_path, "{}s".format(resource), "{}-{}.yaml".format(self.app_name, resource))))
            try:
                update_out = subprocess.check_output(cmd)
            except:
                logging.error("Applying the {} failed:".format(resource))
                sys.exit(1)


def main():
    a = Ausroller()
    resources = a.prepare_rollout()
    a.rollout(resources)

    logging.debug(a.__dict__)

if __name__ == '__main__':
    main()
