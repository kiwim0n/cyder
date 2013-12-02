#!/usr/bin/python
import inspect
import shutil
import syslog
import os
import re
import time
from distutils.dir_util import copy_tree
from gettext import gettext as _
from itertools import ifilter

from cyder.settings import BINDBUILD

from cyder.base.vcs import GitRepo
from cyder.base.utils import (dict_merge, log, MutexMixin, remove_dir_contents,
                              run_command, set_attrs, shell_out)

from cyder.core.task.models import Task
from cyder.core.utils import fail_mail

from cyder.cydns.domain.models import SOA
from cyder.cydns.view.models import View
from cyder.cydns.cybind.zone_builder import build_zone_data
from cyder.cydns.cybind.models import DNSBuildRun
from cyder.cydns.cybind.serial_utils import get_serial


class BuildError(Exception):
    """Exception raised when there is an error in the build process."""


class DNSBuilder(MutexMixin):
    """
    DNSBuilder must be instantiated from a `with` statement. Its __exit__
    method releases a mutex, so it's critical that it be called regardless of
    the reason the Python interpreter exits. __del__ is not sufficient because
    CPython is not guaranteed to delete objects involved in circular
    references even when every object in the circular reference goes out of
    scope.
    """
    def __init__(self, **kwargs):
        kwargs = dict_merge(BINDBUILD, {
            'stage_only': False,
            'clobber_stage': False,
            'push_to_prod': False,
            'build_zones': True,
            'preserve_stage': False,
            'debug': False,
            'force': False,
            'bs': DNSBuildRun(),  # Build statistic
        }, kwargs)
        set_attrs(self, kwargs)

        if self.log_syslog:
            syslog.openlog('dnsbuild', 0, syslog.LOG_LOCAL6)

        self.repo = GitRepo(self.prod_dir, self.max_allowed_lines_changed,
            debug=self.debug, log_syslog=self.log_syslog, logger=syslog)

    def log(self, msg, *args, **kwargs):
        kwargs = dict_merge({
            'to_syslog': self.log_syslog,
            'logger': syslog,
        }, kwargs)

        curframe = inspect.currentframe()
        calframe = inspect.getouterframes(curframe, 2)
        callername = "[{0}]".format(calframe[2][3])

        root_domain = kwargs.pop('root_domain', None)
        if root_domain:
            fullmsg = "{0:24} < {1} > {2}".format(callername,
                                                  root_domain.name, msg)
        else:
            fullmsg = "{0:24} {1}".format(callername, msg)

        log(fullmsg, *args, **kwargs)

    def log_debug(self, msg, root_domain=None, to_stderr=None):
        if to_stderr is None:
            to_stderr = self.debug
        self.log(msg, log_level='LOG_DEBUG', to_stderr=to_stderr,
                 root_domain=root_domain)

    def log_info(self, msg, root_domain=None, to_stderr=True):
        self.log(msg, log_level='LOG_INFO', to_stderr=to_stderr,
                 root_domain=root_domain)

    def log_notice(self, msg, root_domain=None, to_stderr=True):
        self.log(msg, log_level='LOG_NOTICE', to_stderr=to_stderr,
                 root_domain=root_domain)

    def log_err(self, msg, root_domain=None, to_stderr=True):
        self.log(msg, log_level='LOG_ERR', to_stderr=to_stderr,
                 root_domain=root_domain)

    def run_command(self, command, log=True, failure_msg=None):
        if log:
            command_logger = self.log_debug
            failure_logger = self.log_err
        else:
            command_logger = None
            failure_logger = None

        try:
            return run_command(command, command_logger=command_logger,
                               failure_logger=failure_logger,
                               failure_msg=failure_msg)
        except Exception as e:
            raise BuildError(e.message)

    def status(self):
        """Print the status of the build system"""

        for name in dir(self):
            value = getattr(self, name)
            if isinstance(value, (bool, basestring, int, long)):
                print u'{0} = {1}'.format(name, unicode(value))

        #print "is_locked={0}".format(self.is_locked())
        #print "lock_file={0}".format(self.lock_file)
        #print ("stop_file_exists={0}"
               #.format(os.path.exists(self.stop_file)))
        #print "stop_file={0}".format(self.stop_file)
        #print "stage_dir_exists={0}".format(os.path.exists(self.stage_dir))
        #print "stage_dir={0}".format(self.stage_dir)
        #print "prod_dir_exists={0}".format(os.path.exists(self.prod_dir))
        #print "prod_dir={0}".format(self.prod_dir)
        #print "last_run_file={0}".format(self.last_run_file)

    def format_title(self, title):
        return "{0} {1} {0}".format('=' * ((30 - len(title)) / 2), title)

    def get_scheduled(self):
        """
        Find all dns tasks that indicate we need to rebuild a certain zone.
        Evalutate the queryset so nothing slips in (our DB isolation *should*
        cover this). This will ensure that if a record is changed during the
        build it's build request will not be deleted and will be serviced
        during the next build.

        If the build is successful we will delete all the scheduled tasks
        return by this function

        note::
            When we are not checking files into Git we do not need to delete
            the scheduled tasks. Not checking files into Git is indicative of a
            troubleshoot build.
        """
        ts = list(Task.dns.all())
        ts_len = len(ts)
        self.log_debug("{0} zone{1} requested to be rebuilt".format(
            ts_len, 's' if ts_len != 1 else '')
        )
        return ts

    def calc_target(self, root_domain, soa):
        """
        This function decides which directory a zone's zone files go. The
        following is used to decide on the target directory.

        If `root_domain` is a forward domain:

            * Replace all '.' characters with '/' characters.

        If `root_domain` is a reverse domain:

            If it's ipv4:

                'reverse/in-addr.arpa/'
            If it's ipv6:

                'reverse/in-addr.ipv6/'

        The build scripts will create this path on the filesystem if it does
        not exist.

        A relative path is returned by this function.
        """
        if root_domain.is_reverse:
            if root_domain.name.endswith('ipv6'):
                zone_path = "reverse/in-addr.arpa/"
            elif root_domain.name.endswith('arpa'):
                zone_path = "reverse/in-addr.arpa/"
            else:
                raise Exception('Invalid root domain "{0}"'
                                .format(root_domain))
        else:
            tmp_path = '/'.join(reversed(root_domain.name.split('.')))
            zone_path = tmp_path + '/'
        return zone_path

    def write_stage_zone(self, stage_fname, root_domain, fname, data):
        """
        Write a zone_file.
        Return the path to the file.
        """
        if not os.path.exists(os.path.dirname(stage_fname)):
            os.makedirs(os.path.dirname(stage_fname))
        self.log_debug("Stage zone file is {0}".format(stage_fname),
                 root_domain=root_domain)
        with open(stage_fname, 'w+') as fd:
            fd.write(data)
        return stage_fname

    def run_checkzone(self, zone_file, root_domain):
        """Shell out and call named-checkzone on the zone file. If it returns
        with errors raise a BuildError.
        """
        # Check the zone file.
        self.run_command(
            ' '.join((self.named_checkzone, self.named_checkzone_opts,
                      root_domain.name, zone_file)),
            log=True,
            failure_msg='named-checkzone failed on zone {0}'
                        .format(root_domain.name)
        )

    def run_checkconf(self, conf_file):
        self.run_command(
            ' '.join((self.named_checkconf, conf_file)),
            failure_msg='named-checkconf rejected config {0}'.format(conf_file)
        )

    def write_stage_config(self, config_fname, stmts):
        """
        Write config files to the correct area in staging.
        Return the path to the file.
        """
        stage_config = os.path.join(self.stage_dir, "config", config_fname)

        if not os.path.exists(os.path.dirname(stage_config)):
            os.makedirs(os.path.dirname(stage_config))
        with open(stage_config, 'w+') as fd:
            fd.write(stmts)
        return stage_config

    def build_zone(self, view, file_meta, view_data, root_domain):
        """
        This function will write the zone's zone file to the the staging area
        and call named-checkconf on the files.
        """
        stage_fname = os.path.join(self.stage_dir, file_meta['rel_fname'])
        self.write_stage_zone(
            stage_fname, root_domain, file_meta['rel_fname'], view_data
        )
        self.log_debug(
            "Built stage_{0}_file to {1}".format(view.name, stage_fname),
            root_domain=root_domain)
        self.run_checkzone(stage_fname, root_domain)

    def calc_fname(self, view, root_domain):
        return "{0}.{1}".format(root_domain.name, view.name)

    def render_zone_stmt(self, soa, zone_name, file_meta):
        zone_stmt = "zone \"{0}\" IN {{{{\n".format(zone_name)
        zone_stmt += "\ttype {ztype};\n"  # We'll format this later
        if soa.is_signed:
            zone_stmt += "\tfile \"{0}.signed\";\n".format(
                file_meta['bind_fname']
            )
        else:
            zone_stmt += "\tfile \"{0}\";\n".format(
                file_meta['bind_fname']
            )
        zone_stmt += "}};\n"
        return zone_stmt

    def verify_previous_build(self, file_meta, view, root_domain, soa):
        force_rebuild, new_serial = False, None
        serial = get_serial(os.path.join(file_meta['prod_fname']))
        if not serial.isdigit():
            new_serial = int(time.time())
            force_rebuild = True
            # it's a new serial
            self.log_debug(
                "{0} appears to be a new zone. Building {1} "
                "with initial serial {2}".format(soa, file_meta['prod_fname'],
                                                 new_serial),
                root_domain=root_domain)
        elif int(serial) != soa.serial:
            # Looks like someone made some changes... let's nuke them.
            # We should probably email someone too.
            self.log_notice(
                "{0} has serial {1} in svn ({2}) and serial "
                "{3} in the database. Zone will be rebuilt."
                .format(soa, serial, file_meta['prod_fname'],
                        soa.serial),
                root_domain=root_domain)
            force_rebuild = True
            # Choose the highest serial so any slave nameservers don't get
            # confused.
            new_serial = max(int(serial), soa.serial)

        return force_rebuild, new_serial

    def get_file_meta(self, view, root_domain, soa):
        """
        This function trys to pull all file login into one place.
        Files:
            * rel_zone_dir
                - This is the directory path to where the zone file will be
                  placed. It's relative to where the script things the SVN root
                  is. See :func:`calc_target` for more info.
            * fname
                - This is the name of the file, which is usually in the format
                  <zone-name>.<view-name>. See :func:`calc_fname` for more
                  info.
            * rel_fname
                - The joining of rel_zone_dir + fname

            * prod_fname
                - Where the final zone file will be written (full path name)

            * bind_fnam
                - The path name used in the zones `zone` statement. See
                  :func:`render_zone_stmt` for more info.
        """
        file_meta = {}
        rel_zone_dir = self.calc_target(root_domain, soa)
        file_meta['fname'] = self.calc_fname(view, root_domain)
        file_meta['rel_fname'] = os.path.join(rel_zone_dir, file_meta['fname'])
        file_meta['prod_fname'] = os.path.join(self.prod_dir,
                                               file_meta['rel_fname'])
        file_meta['bind_fname'] = os.path.join(self.bind_prefix,
                                               file_meta['rel_fname'])
        return file_meta

    def build_zone_files(self, soa_pks_to_rebuild):
        zone_stmts = {}

        for soa in SOA.objects.all():
            # If anything happens during this soa's build we need to mark
            # it as dirty so it can be rebuild
            try:
                root_domain = soa.root_domain  # This is an expensive lookup

                if not root_domain:
                    continue

                # General order of things:
                # * Find which views should have a zone file built and add them
                # to a list.
                # * If any of the view's zone file have been tampered with or
                # the zone is new, trigger the rebuilding of all the zone's
                # view files. (rebuil all views in a zone keeps the serial
                # synced across all views)
                # * Either rebuild all of a zone's view files because one view
                # needed to be rebuilt due to tampering or the zone was dirty
                # (again, this is to keep their serial synced) or just call
                # named-checkzone on the existing zone files for good measure.
                # Also generate a zone statement and add it to a dictionary for
                # later use during BIND configuration generation.

                force_rebuild = soa.pk in soa_pks_to_rebuild or soa.dirty
                if force_rebuild:
                    soa.dirty = False
                    soa.save()

                self.log_debug('====== Processing {0} {1} ======'.format(
                    root_domain, soa.serial)
                )
                views_to_build = []
                self.log_debug(
                    "SOA was seen with dirty == {0}".format(force_rebuild),
                    root_domain=root_domain
                )

                # This for loop decides which views will be canidates for
                # rebuilding.
                for view in View.objects.all():
                    self.log_debug("++++++ Looking at < {0} > view ++++++".
                             format(view.name), root_domain=root_domain)
                    t_start = time.time()  # tic
                    view_data = build_zone_data(view, root_domain, soa,
                                                logf=self.log_debug)
                    build_time = time.time() - t_start  # toc
                    self.log_debug('< {0} > Built {1} data in {2} seconds'
                             .format(view.name, soa, build_time),
                             root_domain=root_domain)
                    if not view_data:
                        self.log_debug('< {0} > No data found in this view. '
                                 'No zone file will be made or included in any'
                                 ' config for this view.'.format(view.name),
                                 root_domain=root_domain)
                        continue
                    self.log_debug('< {0} > Non-empty data set for this '
                             'view. Its zone file will be included in the '
                             'config.'.format(view.name),
                             root_domain=root_domain)
                    file_meta = self.get_file_meta(view, root_domain, soa)
                    was_bad_prev, new_serial = self.verify_previous_build(
                        file_meta, view, root_domain, soa
                    )

                    if was_bad_prev:
                        soa.serial = new_serial
                        force_rebuild = True

                    views_to_build.append(
                        (view, file_meta, view_data)
                    )

                self.log_debug(
                    '----- Building < {0} > ------'.format(
                        ' | '.join([v.name for v, _, _ in views_to_build])
                    ), root_domain=root_domain
                )

                if force_rebuild:
                    # Bypass save so we don't have to save a possible stale
                    # 'dirty' value to the db.
                    SOA.objects.filter(pk=soa.pk).update(serial=soa.serial + 1)
                    self.log_debug('Zone will be rebuilt at serial {0}'
                             .format(soa.serial + 1), root_domain=root_domain)
                else:
                    self.log_debug('Zone is stable at serial {0}'
                             .format(soa.serial), root_domain=root_domain)

                for view, file_meta, view_data in views_to_build:
                    view_zone_stmts = zone_stmts.setdefault(view.name, [])
                    # If we see a view in this loop it's going to end up in the
                    # config
                    view_zone_stmts.append(
                        self.render_zone_stmt(soa, root_domain, file_meta)
                    )
                    # If it's dirty or we are rebuilding another view, rebuild
                    # the zone
                    if force_rebuild:
                        self.log_debug(
                            'Rebuilding < {0} > view file {1}'
                            .format(view.name, file_meta['prod_fname']),
                            root_domain=root_domain)
                        self.build_zone(
                            view, file_meta,
                            # Lazy string evaluation
                            view_data.format(serial=soa.serial + 1),
                            root_domain
                        )
                    else:
                        self.log_debug(
                            'NO REBUILD needed for < {0} > view file {1}'
                            .format(view.name, file_meta['prod_fname']),
                            root_domain=root_domain
                        )
                        # Run named-checkzone for good measure.
                        if self.stage_only:
                            self.log_debug("Not calling named-checkconf.",
                                     root_domain=root_domain)
                        else:
                            self.run_checkzone(
                                file_meta['prod_fname'], root_domain
                            )
            except Exception:
                soa.schedule_rebuild()
                raise

        return zone_stmts

    def build_view_config(self, view_name, ztype, stmts):
        config_fname = "{0}.{1}".format(ztype, view_name)
        zone_stmts = '\n'.join(stmts).format(ztype=ztype)
        stage_config = self.write_stage_config(config_fname, zone_stmts)
        self.run_checkconf(stage_config)

    def build_config_files(self, zone_stmts):
        # named-checkconf on config files
        self.log_info("Building config files")
        self.log_debug(
            "Building configs for views < {0} >".format(
                ' | '.join([view_name for view_name in zone_stmts.keys()])
            )
        )
        for view_name, view_stmts in zone_stmts.iteritems():
            self.log_debug("Building config for view < {0} >".
                     format(view_name))
            self.build_view_config(view_name, 'master', view_stmts)

    def build(self, clean_up=True, force=False):
        try:
            with open(self.stop_file) as stop_fd:
                now = time.time()
                contents = stop_fd.read()
            last = os.path.getmtime(self.stop_file)

            msg = ("The stop file ({0}) exists. Build canceled.\n"
                   "Reason for skipped build:\n"
                   "{1}".format(self.stop_file, contents))
            self.log_info(msg, to_stderr=False)
            if now - last > self.stop_file_email_interval:
                os.utime(self.stop_file, (now, now))
                fail_mail(msg, subject="DNS builds have stopped")

            raise BuildError(msg)
        except IOError as e:
            if e.errno == 2:  # IOError: [Errno 2] No such file or directory
                pass
            else:
                raise

        self.log_debug('Building...')

        try:
            remove_dir_contents(self.stage_dir)
            dns_tasks = self.get_scheduled()

            if not dns_tasks and not force:
                self.log_info("Nothing to do!")
                return

            # zone files
            soa_pks_to_rebuild = set(int(t.task) for t in dns_tasks)
            self.build_config_files(
                self.build_zone_files(soa_pks_to_rebuild)
            )

            if clean_up:
                # Only delete the scheduled tasks we saw at the top of the
                # function
                map(lambda t: t.delete(), dns_tasks)

            self.log_info('Build successful')
        except (BuildError, Exception):
            self.log_err('Error during build.', to_stderr=False)
            raise

    def push(self, sanity_check=True):
        self.repo.reset_and_pull()

        try:
            copy_tree(self.stage_dir, self.prod_dir)
        except:
            self.repo.clean()
            raise

        self.repo.commit_and_push('Update config file',
                                  sanity_check=sanity_check)

    def _lock_failure(self):
        self.log_err(
            'DNS build script attempted to acquire the build mutux but '
            'another process already has it.',
            to_stderr=False)
        fail_mail(
            'An attempt was made to start the DNS build script while an '
            'instance of the script was already running. The attempt was '
            'denied.',
            subject="Concurrent DNS builds attempted.")
        super(DNSBuilder, self)._lock_failure()
