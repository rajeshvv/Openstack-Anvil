# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import io
import yaml

from anvil import cfg
from anvil import colorizer
from anvil import components as comp
from anvil import log as logging
from anvil import shell as sh
from anvil import utils

from anvil.components.helpers import db as dbhelper
from anvil.components.helpers import glance as ghelper
from anvil.components.helpers import keystone as khelper
from anvil.components.helpers import nova as nhelper
from anvil.components.helpers import quantum as qhelper

LOG = logging.getLogger(__name__)

# This db will be dropped then created
DB_NAME = "keystone"

# This yaml file controls keystone initialization
INIT_WHAT_FN = 'init_what.yaml'

# Existence of this file signifies that initialization ran
INIT_WHAT_HAPPENED = "keystone.inited.yaml"

# Configuration files keystone expects...
ROOT_CONF = "keystone.conf"
LOGGING_CONF = "logging.conf"
POLICY_JSON = 'policy.json'
CONFIGS = [ROOT_CONF, LOGGING_CONF, POLICY_JSON]

# Sync db command
SYNC_DB_CMD = [sh.joinpths('%BIN_DIR%', 'keystone-manage'),
                '--config-file=%CONFIG_FILE%',
                '--debug', '-v',
                # Available commands:
                # db_sync: Sync the database.
                # export_legacy_catalog: Export the service catalog from a legacy database.
                # import_legacy: Import a legacy database.
                # import_nova_auth: Import a dump of nova auth data into keystone.
                'db_sync']

# What to start
APP_NAME = 'keystone-all'
APP_OPTIONS = {
    APP_NAME: ['--config-file=%s' % (sh.joinpths('%CONFIG_DIR%', ROOT_CONF)),
                "--debug", '-v',
                '--log-config=%s' % (sh.joinpths('%CONFIG_DIR%', LOGGING_CONF))],
}


class KeystoneUninstaller(comp.PythonUninstallComponent):
    def __init__(self, *args, **kargs):
        comp.PythonUninstallComponent.__init__(self, *args, **kargs)


class KeystoneInstaller(comp.PythonInstallComponent):
    def __init__(self, *args, **kargs):
        comp.PythonInstallComponent.__init__(self, *args, **kargs)
        self.bin_dir = sh.joinpths(self.get_option('app_dir'), 'bin')

    def _filter_pip_requires_line(self, line):
        if line.lower().find('keystoneclient') != -1:
            return None
        if line.lower().find('ldap') != -1:
            return None
        if line.lower().find('http://tarballs.openstack.org') != -1:
            return None
        if line.lower().find('memcached') != -1:
            return None
        return line

    def post_install(self):
        comp.PythonInstallComponent.post_install(self)
        self._setup_db()
        self._sync_db()

    def _sync_db(self):
        LOG.info("Syncing keystone to database: %s", colorizer.quote(DB_NAME))
        cmds = [{'cmd': SYNC_DB_CMD, 'run_as_root': True}]
        utils.execute_template(*cmds, cwd=self.bin_dir, params=self.config_params(None))

    @property
    def env_exports(self):
        params = khelper.get_shared_params(self.cfg)
        to_set = dict()
        to_set['OS_PASSWORD'] = params['admin_password']
        to_set['OS_TENANT_NAME'] = params['demo_tenant']
        to_set['OS_USERNAME'] = params['demo_user']
        to_set['OS_AUTH_URL'] = params['endpoints']['public']['uri']
        to_set['SERVICE_ENDPOINT'] = params['endpoints']['admin']['uri']
        return to_set

    @property
    def config_files(self):
        return list(CONFIGS)

    def _setup_db(self):
        dbhelper.drop_db(self.cfg, self.distro, DB_NAME)
        dbhelper.create_db(self.cfg, self.distro, DB_NAME, utf8=True)

    def source_config(self, config_fn):
        real_fn = config_fn
        if config_fn == LOGGING_CONF:
            real_fn = 'logging.conf.sample'
        elif config_fn == ROOT_CONF:
            real_fn = "keystone.conf.sample"
        fn = sh.joinpths(self.get_option('app_dir'), 'etc', real_fn)
        return (fn, sh.load_file(fn))

    def _config_adjust_logging(self, contents, fn):
        with io.BytesIO(contents) as stream:
            config = cfg.RewritableConfigParser()
            config.readfp(stream)
            config.set('logger_root', 'level', 'DEBUG')
            config.set('logger_root', 'handlers', "devel,production")
            contents = config.stringify(fn)
        return contents

    def _config_param_replace(self, config_fn, contents, parameters):
        if config_fn in [ROOT_CONF, LOGGING_CONF]:
            # We handle these ourselves
            return contents
        else:
            return comp.PythonInstallComponent._config_param_replace(self, config_fn, contents, parameters)

    def _config_adjust_root(self, contents, fn):
        params = khelper.get_shared_params(self.cfg)
        with io.BytesIO(contents) as stream:
            config = cfg.RewritableConfigParser()
            config.readfp(stream)
            config.set('DEFAULT', 'admin_token', params['service_token'])
            config.set('DEFAULT', 'admin_port', params['endpoints']['admin']['port'])
            config.set('DEFAULT', 'public_port', params['endpoints']['public']['port'])
            config.set('DEFAULT', 'verbose', True)
            config.set('DEFAULT', 'debug', True)
            config.set('catalog', 'driver', 'keystone.catalog.backends.sql.Catalog')
            config.remove_option('DEFAULT', 'log_config')
            config.set('sql', 'connection', dbhelper.fetch_dbdsn(self.cfg, DB_NAME, utf8=True))
            config.set('ec2', 'driver', "keystone.contrib.ec2.backends.sql.Ec2")
            contents = config.stringify(fn)
        return contents

    def _config_adjust(self, contents, name):
        if name == ROOT_CONF:
            return self._config_adjust_root(contents, name)
        elif name == LOGGING_CONF:
            return self._config_adjust_logging(contents, name)
        else:
            return contents

    def warm_configs(self):
        khelper.get_shared_params(self.cfg)

    def config_params(self, config_fn):
        # These be used to fill in the configuration params
        mp = comp.PythonInstallComponent.config_params(self, config_fn)
        mp['BIN_DIR'] = self.bin_dir
        mp['CONFIG_FILE'] = sh.joinpths(self.get_option('cfg_dir'), ROOT_CONF)
        return mp


class KeystoneRuntime(comp.PythonRuntime):
    def __init__(self, *args, **kargs):
        comp.PythonRuntime.__init__(self, *args, **kargs)
        self.bin_dir = sh.joinpths(self.get_option('app_dir'), BIN_DIR)
        self.wait_time = max(self.cfg.getint('DEFAULT', 'service_wait_seconds'), 1)
        self.init_fn = sh.joinpths(self.get_option('trace_dir'), INIT_WHAT_HAPPENED)

    def post_start(self):
        if not sh.isfile(self.init_fn) and self.get_option('do-init'):
            LOG.info("Waiting %s seconds so that keystone can start up before running first time init." % (self.wait_time))
            sh.sleep(self.wait_time)
            LOG.info("Running commands to initialize keystone.")
            LOG.debug("Initializing with %s", self.init_what)
            cfg = {
                'glance': ghelper.get_shared_params(self.cfg),
                'keystone': khelper.get_shared_params(self.cfg),
                'nova': nhelper.get_shared_params(self.cfg),
                'quantum': qhelper.get_shared_params(self.cfg),
            }
            (_fn, contents) = utils.load_template(self.name, INIT_WHAT_FN)
            init_what = utils.param_replace_deep(copy.deepcopy(yaml.load(contents)), cfg)
            khelper.Initializer(initial_cfg['keystone']).initialize(**init_what)
            # Writing this makes sure that we don't init again
            sh.write_file(self.init_fn, utils.prettify_yaml(init_what))
            LOG.info("If you wish to re-run initialization, delete %s", colorizer.quote(self.init_fn))

    @property
    def apps_to_start(self):
        apps = []
        for app_name in APP_OPTIONS.keys():
            apps.append({
                'name': app_name,
                'path': sh.joinpths(self.bin_dir, app_name),
            })
        return apps

    def app_options(self, app):
        return APP_OPTIONS.get(app)
