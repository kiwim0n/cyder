from string import Template
from gettext import gettext as _

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import models

from cyder.base.utils import transaction_atomic
from cyder.cydhcp.interface.static_intr.models import StaticInterface
from cyder.cydns.domain.models import Domain
from cyder.cydns.address_record.models import AddressRecord
from cyder.cydns.validation import validate_label, validate_fqdn
from cyder.cydns.view.models import View
from cyder.cydns.models import CydnsRecord


class Nameserver(CydnsRecord):
    """
    Name server for forward domains::

        >>> Nameserver(domain = domain, server = server)

        Sometimes a name server needs a glue record. A glue record can either
        be an AddressRecord or a StaticInterface. These two types are
        represented but the attributes `addr_glue` and `intr_glue`, which are
        both FK's enforced by the DB.

        If there are two A or two Interfaces, or one A and one Interface that
        fit the criterion of being a NS's glue record, the user should have the
        choice to choose between records. Because of this, a glue record will
        only be automatically assigned to a NS if a) The NS doesn't have a glue
        record or b) the glue record the NS has isn't valid.
    """
    @property
    def pretty_name(self):
        return self.server

    id = models.AutoField(primary_key=True)
    domain = models.ForeignKey(Domain, null=False, help_text="The domain this "
                               "record is for.")
    server = models.CharField(max_length=255, validators=[validate_fqdn],
                              help_text="The name of the server this records "
                              "points to.")
    # "If the name server does lie within the domain it should have a
    # corresponding A record."
    addr_glue = models.ForeignKey(AddressRecord, null=True, blank=True,
                                  related_name="nameserver_set")
    intr_glue = models.ForeignKey(StaticInterface, null=True, blank=True,
                                  related_name="nameserver_set")

    template = _("{bind_name:$lhs_just} {ttl:$ttl_just}  "
                 "{rdclass:$rdclass_just} "
                 "{rdtype:$rdtype_just} {server:$rhs_just}.")

    search_fields = ("server", "domain__name")

    class Meta:
        app_label = 'cyder'
        db_table = "nameserver"
        unique_together = ("domain", "server")

    def __unicode__(self):
        return u'{} NS {}'.format(self.domain.name, self.server)

    @staticmethod
    def filter_by_ctnr(ctnr, objects=None):
        objects = objects or Nameserver.objects
        objects = objects.filter(domain__in=ctnr.domains.all())
        return objects

    def get_ctnrs(self):
        raise TypeError("This object has no container.")

    @property
    def rdtype(self):
        return 'NS'

    def bind_render_record(self, pk=False, **kwargs):
        # We need to override this because fqdn is actually self.domain.name
        template = Template(self.template).substitute(**self.justs)
        return template.format(
            rdtype=self.rdtype, rdclass='IN', bind_name=self.domain.name + '.',
            **self.__dict__
        )

    def details(self):
        """For tables."""
        data = super(Nameserver, self).details()
        data['data'] = [
            ('Domain', 'domain', self.domain),
            ('Server', 'server', self.server),
            ('Glue', None, self.get_glue()),
        ]
        return data

    @staticmethod
    def eg_metadata():
        """EditableGrid metadata."""
        return {'metadata': [
            {'name': 'domain', 'datatype': 'string', 'editable': False},
            {'name': 'server', 'datatype': 'string', 'editable': True},
            {'name': 'glue', 'datatype': 'string', 'editable': True},
        ]}

    # TODO, make this a property
    def get_glue(self):
        if self.addr_glue:
            return self.addr_glue
        elif self.intr_glue:
            return self.intr_glue
        else:
            return None

    def set_glue(self, glue):
        if isinstance(glue, AddressRecord):
            self.addr_glue = glue
            self.intr_glue = None
        elif isinstance(glue, StaticInterface):
            self.addr_glue = None
            self.intr_glue = glue
        elif isinstance(glue, type(None)):
            self.addr_glue = None
            self.intr_glue = None
        else:
            raise ValueError("Cannot assing {0}: Nameserver.glue must be of "
                             "either type AddressRecord or type "
                             "StaticInterface.".format(glue))

    @transaction_atomic
    def del_glue(self):
        if self.addr_glue:
            self.addr_glue.delete(commit=False)
        elif self.intr_glue:
            self.intr_glue.delete(commit=False)
        else:
            raise AttributeError("'Nameserver' object has no attribute 'glue'")

    glue = property(get_glue, set_glue, del_glue, "The Glue property.")

    @transaction_atomic
    def delete(self, *args, **kwargs):
        self.check_no_ns_soa_condition(self.domain)
        super(Nameserver, self).delete(*args, **kwargs)
        if self.get_glue():
            self.del_glue()

    @transaction_atomic
    def save(self, *args, **kwargs):
        self.full_clean()

        super(Nameserver, self).save(*args, **kwargs)

    def clean(self, *args, **kwargs):
        try:
            self.domain
        except Domain.DoesNotExist:
            return  # clean_fields already seen `domain`'s own ValidationError.

        self.check_for_cname()

        if not self.needs_glue():
            self.glue = None
        else:
            # Try to find any glue record. It will be the first eligible
            # The resolution is:
            #  * Address records are searched.
            #  * Interface records are searched.
            # AddressRecords take higher priority over interface records.
            server = self.server.strip(".").lower()
            if server == self.domain.name.lower():
                glue_label = ""
            else:
                glue_label = server[:server.find('.')]  # foo.com -> foo
            fqdn = ".".join([glue_label, self.domain.name]).strip(".")

            if (self.glue and self.glue.label == glue_label and
                    self.glue.domain == self.domain):
                # Our glue record is valid. Don't go looking for a new one.
                pass
            else:
                # Ok, our glue record wasn't valid, let's find a new one.
                addr_glue = AddressRecord.objects.filter(fqdn=fqdn)
                intr_glue = StaticInterface.objects.filter(fqdn=fqdn)
                if not (addr_glue or intr_glue):
                    raise ValidationError(
                        "This NS needs a glue record. Create a glue "
                        "record for the server before creating "
                        "the NS record."
                    )
                else:
                    if addr_glue:
                        self.glue = addr_glue[0]
                    else:
                        self.glue = intr_glue[0]

    def clean_views(self, views):
        # Forms will call this function with the set of views it is about to
        # set on this object. Make sure we aren't serving as the NS for a view
        # that we are about to remove.
        removed_views = set(View.objects.all()) - set(views)
        for view in removed_views:
            if (self.domain.soa and
                self.domain.soa.root_domain == self.domain and
                self.domain.nameserver_set.filter(views=view).count() == 1 and
                # We are it!
                    self.domain.soa.has_record_set(exclude_ns=True,
                                                   view=view)):
                raise ValidationError(
                    "Other records in this nameserver's zone are "
                    "relying on it's existance in the {0} view. You can't "
                    "remove it's memebership of the {0} view.".format(view)
                )

    def check_no_ns_soa_condition(self, domain):
        if (domain.soa and
            domain.soa.root_domain == domain and
            domain.nameserver_set.count() == 1 and  # We are it!
                domain.soa.has_record_set(exclude_ns=True)):
            raise ValidationError(
                "Other records in this nameserver's zone are "
                "relying on it's existance as it is the only nameserver "
                "at the root of the zone."
            )

    def needs_glue(self):
        # Replace the domain portion of the server with "".
        # if domain == foo.com and server == ns1.foo.com.
        #       ns1.foo.com --> ns1
        server = self.server.strip('.').lower()
        if server == self.domain.name.lower():
            if self.domain.delegated:
                return True
            else:
                return False

        try:
            possible_label = server.replace("." + self.domain.name, "")
        except ObjectDoesNotExist:
            return False

        if possible_label == server:
            return False
        try:
            validate_label(possible_label)
        except ValidationError:
            # It's not a valid label
            return False
        return True
