from gettext import gettext as _

from django.db import models

from cyder.base.utils import transaction_atomic
from cyder.cydns.domain.models import Domain
from cyder.cydns.models import CydnsRecord, LabelDomainUtilsMixin
from cyder.cydns.validation import (
    validate_srv_label, validate_srv_port, validate_srv_priority,
    validate_srv_weight, validate_srv_name, validate_srv_target
)


class SRV(CydnsRecord, LabelDomainUtilsMixin):
    """
    >>> SRV(label=label, domain=domain, target=target, port=port,
    ... priority=priority, weight=weight, ttl=ttl)
    """

    pretty_type = 'SRV'

    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=63, blank=True,
                             validators=[validate_srv_label],
                             help_text="Short name of the FQDN")
    domain = models.ForeignKey(Domain, null=False,
                               limit_choices_to={'is_reverse': False})
    fqdn = models.CharField(max_length=255, blank=True,
                            validators=[validate_srv_name])
    target = models.CharField(max_length=100,
                              validators=[validate_srv_target], blank=True)
    port = models.PositiveIntegerField(null=False,
                                       validators=[validate_srv_port])
    priority = models.PositiveIntegerField(null=False,
                                           validators=[validate_srv_priority])
    weight = models.PositiveIntegerField(null=False,
                                         validators=[validate_srv_weight])
    ctnr = models.ForeignKey("cyder.Ctnr", null=False,
                             verbose_name="Container")

    template = _("{bind_name:$lhs_just} {ttl:$ttl_just}  "
                 "{rdclass:$rdclass_just} "
                 "{rdtype:$rdtype_just} {priority:$prio_just} "
                 "{weight:$extra_just} {port:$extra_just} "
                 "{target:$extra_just}.")

    search_fields = ("fqdn", "target")

    def details(self):
        """For tables."""
        data = super(SRV, self).details()
        data['data'] = [
            ('Label', 'label', self.label),
            ('Domain', 'domain__name', self.domain),
            ('Target', 'target', self.target),
            ('Port', 'port', self.port),
            ('Priority', 'priority', self.priority),
            ('Weight', 'weight', self.weight),
        ]
        return data

    def __unicode__(self):
        return u'{} SRV {}'.format(self.fqdn, self.target)

    @staticmethod
    def eg_metadata():
        """EditableGrid metadata."""
        return {'metadata': [
            {'name': 'label', 'datatype': 'string', 'editable': True},
            {'name': 'domain', 'datatype': 'string', 'editable': True},
            {'name': 'target', 'datatype': 'string', 'editable': True},
            {'name': 'port', 'datatype': 'integer', 'editable': True},
            {'name': 'priority', 'datatype': 'integer', 'editable': True},
            {'name': 'weight', 'datatype': 'integer', 'editable': True},
        ]}

    class Meta:
        app_label = 'cyder'
        db_table = 'srv'
        unique_together = ("label", "domain", "target", "port")

    @property
    def rdtype(self):
        return 'SRV'

    @transaction_atomic
    def save(self, *args, **kwargs):
        self.full_clean()

        super(SRV, self).save(*args, **kwargs)
