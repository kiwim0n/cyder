from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from cyder.base.eav.models import Attribute
from cyder.core.ctnr.models import Ctnr, CtnrUser
from cyder.core.system.models import System, SystemAV
from cyder.cydns.domain.models import Domain
from cyder.cydns.models import View
from cyder.cydhcp.constants import (ALLOW_ANY, ALLOW_KNOWN, ALLOW_LEGACY,
                                    ALLOW_LEGACY_AND_VRF, STATIC, DYNAMIC)
from cyder.cydhcp.interface.dynamic_intr.models import DynamicInterface
from cyder.cydhcp.network.models import Network, NetworkAV
from cyder.cydhcp.range.models import Range, RangeAV
from cyder.cydhcp.site.models import Site
from cyder.cydhcp.vlan.models import Vlan
from cyder.cydhcp.vrf.models import Vrf
from cyder.cydhcp.workgroup.models import Workgroup, WorkgroupAV

from sys import stderr
from random import choice
import ipaddr
import MySQLdb
from optparse import make_option

from lib.utilities import long2ip, fix_attr_name, range_usage_get_create


cached = {}
host_option_values = None

public, _ = View.objects.get_or_create(name="public")
private, _ = View.objects.get_or_create(name="private")

allow_all_subnets = [
    '10.192.76.2', '10.192.103.150', '10.192.15.2',
    '10.197.32.0', '10.192.148.32', '10.192.144.32',  '10.192.140.32',
    '10.196.0.32', '10.196.4.32', '10.192.136.63', '10.196.8.8',
    '10.196.16.8', '10.196.24.8', '10.196.32.8', '10.196.40.8',
    '10.162.128.32', '10.162.136.32', '10.162.144.32', '10.198.0.80',
    '10.198.0.140', '10.192.131.9', '10.255.255.255', '10.214.64.32']


class NotInMaintain(Exception):
    """"""


def calc_prefixlen(netmask):
    bits = 0
    while netmask:
        bits += netmask & 1
        netmask >>= 1
    return bits

connection = MySQLdb.connect(host=settings.MIGRATION_HOST,
                             user=settings.MIGRATION_USER,
                             passwd=settings.MIGRATION_PASSWD,
                             db=settings.MIGRATION_DB,
                             charset='utf8')

cursor = connection.cursor()


def clean_zone_name(name):
    name = name.replace(' ', '')
    #if name[:5] == "zone.":
        #name = name[5:]
    return name


def create_subnet(subnet_id, name, subnet, netmask, status, vlan):
    """
    Takes a row from the Maintain subnet table
    returns a new network object and creates the vlan it is associated with
    """
    prefixlen = str(calc_prefixlen(netmask))
    network = str(ipaddr.IPv4Address(subnet & netmask))
    s, _ = Site.objects.get_or_create(name='Campus')
    v = None
    enabled = (status == 'visible')
    if cursor.execute("SELECT * "
                      "FROM vlan "
                      "WHERE vlan_id = %s" % vlan):
        vlan_id, vlan_name, vlan_number = cursor.fetchone()
        v = Vlan.objects.get(name=vlan_name)
    n, created = Network.objects.get_or_create(
        network_str=network + '/' + prefixlen, ip_type='4',
        site=s, vlan=v, enabled=enabled)
    cursor.execute("SELECT dhcp_option, value "
                   "FROM object_option "
                   "WHERE object_id = {0} "
                   "AND type = 'subnet'".format(subnet_id))
    results = cursor.fetchall()
    for dhcp_option, value in results:
        cursor.execute("SELECT name, type "
                       "FROM dhcp_options "
                       "WHERE id = {0}".format(dhcp_option))
        name, type = cursor.fetchone()
        attr = Attribute.objects.get(name=fix_attr_name(name))
        eav, eav_created = NetworkAV.objects.get_or_create(
            entity=n, attribute=attr, value=value)
        if eav_created:
            eav.full_clean()
            eav.save()
    return (n, created)


def create_range(range_id, start, end, range_type, subnet_id,
                 comment, enabled, known):
    """
    Takes a row from the Maintain range table.
    Returns a range which is saved in Cyder.
    """
    # Set the allow statement
    n = None
    r = None
    d = maintain_find_range_domain(range_id)
    if not d and range_type == "dynamic":
        stderr.write("Ignoring range %s: No default domain.\n" % range_id)
        return None

    r_type = STATIC if range_type == 'static' else DYNAMIC
    allow = ALLOW_LEGACY
    if cursor.execute("SELECT subnet, netmask "
                      "FROM subnet WHERE id = {0}".format(subnet_id)):
        subnet, netmask = cursor.fetchone()
        n = Network.objects.get(ip_lower=subnet,
                                prefixlen=str(calc_prefixlen(netmask)))
        n.update_network()

        if str(ipaddr.IPv4Address(start)) in allow_all_subnets:
            allow = ALLOW_ANY
        elif known:
            allow = ALLOW_KNOWN
        elif '128.193.177.71' == str(ipaddr.IPv4Address(start)):
            allow = ALLOW_LEGACY_AND_VRF
            v, _ = Vrf.objects.get_or_create(name="ip-phones-hack")
            n.vrf = v
            n.save()
        elif '128.193.166.81' == str(ipaddr.IPv4Address(start)):
            allow = ALLOW_LEGACY_AND_VRF
            v, _ = Vrf.objects.get_or_create(name="avaya-hack")
            n.vrf = v
            n.save()

        range_str = "{0} - {1}".format(ipaddr.IPv4Address(start),
                                       ipaddr.IPv4Address(end))

        valid_start = int(n.network.network) < start < int(n.network.broadcast)
        valid_order = start <= end
        valid_end = int(n.network.network) < end < int(n.network.broadcast)

        valid = all((valid_start, valid_order, valid_end))

        # If the range is disabled, we don't need to print warnings.
        if not valid and enabled:
            print 'Range {0} in network {1} is invalid:'.format(range_str, n)

            if not valid_start:
                print ('\tStart is not inside network'
                       .format(n.network.network))
            if not valid_order:
                print '\tStart and end are out of order'
            if not valid_end:
                print ('\tEnd is not inside network'
                       .format(n.network.broadcast))

        dhcp_enabled = bool(enabled and valid)
    else:
        # the Range doesn't have a Network
        n = None
        dhcp_enabled = False

    r, created = range_usage_get_create(
        Range, start_lower=start, start_str=ipaddr.IPv4Address(start),
        end_lower=end, end_str=ipaddr.IPv4Address(end), range_type=r_type,
        allow=allow, ip_type='4', network=n, dhcp_enabled=dhcp_enabled,
        is_reserved=not dhcp_enabled)
    r.views.add(public)
    r.views.add(private)

    if '128.193.166.81' == str(ipaddr.IPv4Address(start)):
        attr = Attribute.objects.get(name=fix_attr_name('ipphone242'))
        eav, eav_created = RangeAV.objects.get_or_create(
            entity=r, attribute=attr, value='L2Q=1,L2QVLAN=503')
        if eav_created:
            eav.full_clean()
            eav.save()

    return (r, created)


def migrate_subnets():
    print "Migrating subnets."
    migrated = []
    cursor.execute("SELECT * FROM subnet")
    results = cursor.fetchall()
    for row in results:
        migrated.append(create_subnet(*row))
    print ("Records in Maintain: {0}\n"
           "Records migrated: {1}\n"
           "Records created: {2}".format(
               len(results),
               len(migrated),
               len([y for x, y in migrated if y])))


def migrate_ranges():
    print "Migrating ranges."
    cursor.execute("SELECT id, start, end, type, subnet, comment, enabled, "
                   "allow_all_hosts "
                   "FROM `ranges`")
    results = cursor.fetchall()
    migrated = []
    for row in sorted(results):
        r = create_range(*row)
        if r:
            migrated.append(r)
    print ("Records in Maintain {0}\n"
           "Records Migrated {1}\n"
           "Records created {2}".format(
               len(results),
               len(migrated),
               len([y for x, y in migrated if y])))


def migrate_vlans():
    print "Migrating VLANs."
    cursor.execute("SELECT * FROM vlan")
    results = cursor.fetchall()
    migrated = []
    for _, name, number in results:
        migrated.append(Vlan.objects.get_or_create(name=name, number=number))
    print ("Records in Maintain {0}\n"
           "Records Migrated {1}\n"
           "Records created {2}".format(
               len(results),
               len(migrated),
               len([y for x, y in migrated if y])))


def migrate_workgroups():
    print "Migrating workgroups."
    cursor.execute("SELECT * FROM workgroup")
    results = cursor.fetchall()
    migrated = []
    for id, name in results:
        w, created = Workgroup.objects.get_or_create(name=name)
        cursor.execute("SELECT dhcp_option, value "
                       "FROM object_option "
                       "WHERE object_id = {0} "
                       "AND type = 'workgroup'".format(id))
        _results = cursor.fetchall()
        for dhcp_option, value in _results:
            cursor.execute("SELECT name, type "
                           "FROM dhcp_options "
                           "WHERE id = {0}".format(dhcp_option))
            name, type = cursor.fetchone()
            attr = Attribute.objects.get(name=fix_attr_name(name))
            eav, eav_created = WorkgroupAV.objects.get_or_create(
                entity=w, attribute=attr, value=value)
            if eav_created:
                eav.clean()
                eav.save()
        migrated.append((w, created))
    print ("Records in Maintain {0}\n"
           "Records Migrated {1}\n"
           "Records created {2}".format(
               len(results),
               len(migrated),
               len([y for x, y in migrated if y])))


def migrate_zones():
    print "Migrating containers."
    cursor.execute("SELECT name, description, comment, "
                   "support_mail, allow_blank_ha "
                   "FROM zone")
    migrated = []
    results = cursor.fetchall()
    for name, desc, comment, email_contact, allow_blank_mac in results:
        name = clean_zone_name(name)

        migrated.append(
            Ctnr.objects.get_or_create(
                name=name,
                description=comment or desc,
                email_contact=email_contact or ''))

    print ("Records in Maintain {0}\n"
           "Records Migrated {1}\n"
           "Records created {2}".format(
               len(results),
               len(migrated),
               len([y for x, y in migrated if y])))


@transaction.commit_on_success
def migrate_dynamic_hosts():
    print "Migrating dynamic hosts."
    default, _ = Workgroup.objects.get_or_create(name='default')

    sys_value_keys = {"type": "Hardware Type",
                      "os": "Operating System",
                      "location": "Location",
                      "department": "Department",
                      "serial": "Serial Number",
                      "other_id": "Other ID",
                      "purchase_date": "Purchase Date",
                      "po_number": "PO Number",
                      "warranty_date": "Warranty Date",
                      "owning_unit": "Owning Unit",
                      "user_id": "User ID"}

    keys = ("id", "dynamic_range", "name", "workgroup", "enabled", "ha",
            "type", "os", "location", "department", "serial", "other_id",
            "purchase_date", "po_number", "warranty_date", "owning_unit",
            "user_id", "last_seen", "expire", "ttl", "last_update", "domain",
            "zone")

    sql = "SELECT %s FROM host WHERE ip = 0" % ", ".join(keys)

    count = 0
    cursor.execute(sql)
    for values in cursor.fetchall():
        items = dict(zip(keys, values))
        enabled = items['enabled']
        mac = items['ha']

        if len(mac) != 12 or mac == '0' * 12:
            mac = ""

        if mac == "":
            enabled = False

        r = None
        if not items['dynamic_range']:
            print ("Host with MAC {0} "
                   "has no dynamic_range in Maintain".format(items['ha']))
        else:
            try:
                r = maintain_find_range(items['dynamic_range'])
            except ObjectDoesNotExist:
                print ("Host with MAC {0} has "
                       "no dynamic range in Cyder".format(items['ha']))

        c = None
        if not items['zone']:
            print "Host with MAC {0} has no zone in Maintain".format(
                item['ha'])
        else:
            try:
                c = maintain_find_zone(items['zone'])
            except ObjectDoesNotExist:
                print ("Host with MAC {0} has "
                       "no ctnr in Cyder".format(items['ha']))
                print "Failed to migrate zone_id {0}".format(items['zone'])

        d = None
        if not items['domain']:
            print "Host with MAC {0} has no domain in Maintain".format(
                items['ha'])
        else:
            try:
                d = maintain_find_domain(items['domain'])
            except ObjectDoesNotExist:
                print ("Host with MAC {0} has "
                       "no domain in Cyder".format(items['ha']))
                print "Failed to migrate domain_id {0}".format(items['domain'])

        w = default
        if items['workgroup']:
            try:
                w = maintain_find_workgroup(items['workgroup'])
            except ObjectDoesNotExist:
                print ("Host with MAC {0} has "
                       "no workgroup in Cyder".format(items['ha']))
                print ("Failed to migrate workgroup_id {0}\n"
                       "Adding it to the default group".format(
                            items['workgroup']))

        if not all([r, c]):
            stderr.write("Trouble migrating host with mac {0}\n"
                         .format(items['ha']))
            continue

        s = System(name=items['name'])
        s.save()
        for key in sys_value_keys.keys():
            value = items[key].strip()
            if not value or value == '0':
                continue

            attr = Attribute.objects.get(
                name=fix_attr_name(sys_value_keys[key]))
            eav = SystemAV(entity=s, attribute=attr, value=value)
            eav.full_clean()
            eav.save()

        intr, _ = range_usage_get_create(
            DynamicInterface, range=r, workgroup=w, ctnr=c, domain=d, mac=mac,
            system=s, dhcp_enabled=enabled, last_seen=items['last_seen'])

        count += 1
        if not count % 1000:
            print "%s valid hosts found so far." % count

    print "%s valid hosts found. Committing transaction." % count


def migrate_user():
    print "Migrating users."
    cursor.execute("SELECT username FROM user "
                   "WHERE username IN ( "
                   "SELECT DISTINCT username FROM zone_user )")
    result = cursor.fetchall()
    for username, in result:
        username = username.lower()
        user, _ = User.objects.get_or_create(username=username)


def migrate_zone_user():
    print "Migrating user-container relationship."
    NEW_LEVEL = {5: 0, 25: 1, 50: 2, 100: 2}
    cursor.execute("SELECT * FROM zone_user")
    result = cursor.fetchall()
    for _, username, zone_id, level in result:
        username = username.lower()
        level = NEW_LEVEL[level]
        user, _ = User.objects.get_or_create(username=username)
        if zone_id == 0:
            ctnr = Ctnr.objects.get(pk=1)
            user.is_superuser = True
            user.save()
        else:
            ctnr = maintain_find_zone(zone_id)

        if not ctnr:
            continue

        CtnrUser.objects.get_or_create(user=user, ctnr=ctnr, level=level)


def migrate_zone_range():
    print "Migrating container-range relationship."
    cursor.execute("SELECT * FROM zone_range")
    result = cursor.fetchall()
    for _, zone_id, range_id, _, comment, _ in result:
        c = maintain_find_zone(zone_id)
        r = maintain_find_range(range_id)
        if not (c and r):
            continue

        c.ranges.add(r)
        c.save()


def migrate_zone_domain():
    print "Migrating container-domain relationship."
    cursor.execute("SELECT zone, domain FROM zone_domain")
    results = cursor.fetchall()
    for zone_id, domain_id in results:
        ctnr = maintain_find_zone(zone_id)
        domain = maintain_find_domain(domain_id)
        if not ctnr or not domain:
            continue

        ctnr.domains.add(domain)
        ctnr.save()


def migrate_zone_reverse():
    print "Migrating container-reverse_domain relationship."
    cursor.execute("SELECT ip,zone FROM pointer WHERE type='reverse'")
    results = cursor.fetchall()
    for ip, zone_id in results:
        ctnr = maintain_find_zone(zone_id)
        if not ctnr:
            continue

        doctets = []
        octets = long2ip(ip).split(".")
        for octet in octets:
            doctets = [octet] + doctets
            dname = ".".join(doctets) + ".in-addr.arpa"
            domain, _ = Domain.objects.get_or_create(name=dname,
                                                     is_reverse=True)

        ctnr.domains.add(domain)
        ctnr.save()


def migrate_zone_workgroup():
    print "Migrating container-workgroup relationship."
    cursor.execute("SELECT * FROM zone_workgroup")
    result = cursor.fetchall()
    for _, workgroup_id, zone_id, _ in result:
        c = maintain_find_zone(zone_id)
        w = maintain_find_workgroup(workgroup_id)
        if not (c and w):
            continue

        c.workgroups.add(w)
        c.save()


def maintain_find_range_domain(range_id):
    sql = ("SELECT default_domain FROM zone_range WHERE `range` = %s;"
           % range_id)
    cursor.execute(sql)
    results = [r[0] for r in cursor.fetchall() if r[0] is not None]
    if results:
        return maintain_find_domain(choice(results))
    else:
        return None


def maintain_find_range(range_id):
    start, end = maintain_get_cached('ranges', ['start', 'end'], range_id)
    if start and end:
        return Range.objects.get(start_lower=start, end_lower=end)


def maintain_find_domain(domain_id):
    (name,) = maintain_get_cached('domain', ['name'], domain_id)
    if name:
        return Domain.objects.get(name=name)


def maintain_find_workgroup(workgroup_id):
    (name,) = maintain_get_cached('workgroup', ['name'], workgroup_id)
    if name:
        return Workgroup.objects.get(name=name)


def maintain_find_zone(zone_id):
    (name,) = maintain_get_cached('zone', ['name'], zone_id)
    if name:
        name = clean_zone_name(name)
        try:
            return Ctnr.objects.get(name=name)
        except Ctnr.DoesNotExist:
            return None


def maintain_get_cached(table, columns, object_id):
    global cached
    columns = tuple(columns)
    if (table, columns) not in cached:
        sql = "SELECT id, %s FROM %s" % (", ".join(columns), table)
        print "Caching: %s" % sql
        cursor.execute(sql)
        results = cursor.fetchall()
        results = [(r[0], tuple(r[1:])) for r in results]
        cached[(table, columns)] = dict(results)

    if object_id in cached[(table, columns)]:
        return cached[(table, columns)][object_id]
    else:
        return (None for _ in columns)


def migrate_all(skip=False):
    migrate_vlans()
    migrate_zones()
    migrate_workgroups()
    migrate_subnets()
    migrate_ranges()
    if not skip:
        migrate_dynamic_hosts()
    migrate_zone_range()
    migrate_zone_workgroup()
    migrate_zone_domain()
    migrate_zone_reverse()
    migrate_user()
    migrate_zone_user()


def delete_all():
    print "Deleting DHCP objects."
    Range.objects.all().delete()
    Vlan.objects.all().delete()
    Network.objects.all().delete()
    Vrf.objects.all().delete()
    Ctnr.objects.filter(id__gt=2).delete()  # First 2 are fixtures
    DynamicInterface.objects.all().delete()
    Workgroup.objects.all().delete()
    User.objects.filter(id__gt=1).delete()  # First user is a fixture
    CtnrUser.objects.filter(id__gt=2).delete()  # First 2 are fixtures


def do_everything(skip=False):
    delete_all()
    migrate_all(skip)


class Command(BaseCommand):

    option_list = BaseCommand.option_list + (
        make_option('-D', '--delete',
                    action='store_true',
                    dest='delete',
                    default=False,
                    help='Delete things'),
        make_option('-a', '--all',
                    action='store_true',
                    dest='all',
                    default=False,
                    help='Migrate everything'),
        make_option('-S', '--skip',
                    action='store_true',
                    dest='skip',
                    default=False,
                    help='Ignore dynamic hosts when using -a option'),
        make_option('-n', '--vlan',
                    action='store_true',
                    dest='vlan',
                    default=False,
                    help='Migrate vlans'),
        make_option('-Z', '--zone',
                    action='store_true',
                    dest='zone',
                    default=False,
                    help='Migrate zones to ctnrs'),
        make_option('-w', '--workgroup',
                    action='store_true',
                    dest='workgroup',
                    default=False,
                    help='Migrate workgroups'),
        make_option('-s', '--subnet',
                    action='store_true',
                    dest='subnet',
                    default=False,
                    help='Migrate subnets'),
        make_option('-r', '--range',
                    action='store_true',
                    dest='range',
                    default=False,
                    help='Migrate ranges'),
        make_option('-d', '--dynamic',
                    action='store_true',
                    dest='dynamic',
                    default=False,
                    help='Migrate dynamic interfaces'),
        make_option('-R', '--zone-range',
                    action='store_true',
                    dest='zone-range',
                    default=False,
                    help='Migrate zone/range relationship'),
        make_option('-W', '--zone-workgroup',
                    action='store_true',
                    dest='zone-workgroup',
                    default=False,
                    help='Migrate zone/workgroup relationship'),
        make_option('-z', '--zone-domain',
                    action='store_true',
                    dest='zone-domain',
                    default=False,
                    help='Migrate zone/domain relationship'),
        make_option('-e', '--zone-reverse',
                    action='store_true',
                    dest='zone-reverse',
                    default=False,
                    help='Migrate zone/reverse domain relationship'),
        make_option('-u', '--user',
                    action='store_true',
                    dest='user',
                    default=False,
                    help='Migrate users'),
        make_option('-U', '--zone-user',
                    action='store_true',
                    dest='zone-user',
                    default=False,
                    help='Migrate zone/user relationship'))

    def handle(self, **options):
        if options['delete']:
            delete_all()
        if options['all']:
            migrate_all(skip=options['skip'])
        else:
            if options['vlan']:
                migrate_vlans()
            if options['zone']:
                migrate_zones()
            if options['workgroup']:
                migrate_workgroups()
            if options['subnet']:
                migrate_subnets()
            if options['range']:
                migrate_ranges()
            if options['dynamic']:
                migrate_dynamic_hosts()
            if options['zone-range']:
                migrate_zone_range()
            if options['zone-workgroup']:
                migrate_zone_workgroup()
            if options['zone-domain']:
                migrate_zone_domain()
            if options['zone-reverse']:
                migrate_zone_reverse()
            if options['user']:
                migrate_user()
            if options['zone-user']:
                migrate_zone_user()