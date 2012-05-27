from django.shortcuts import get_object_or_404
from django.utils.encoding import force_unicode
from django.views.generic.base import TemplateView
from django.utils.translation import ugettext as _
from django.contrib.admin.util import unquote
from django.utils.text import capfirst
from django.contrib.contenttypes.models import ContentType
from django.contrib.admin.views.base import AdminViewMixin


class AdminHistoryView(AdminViewMixin, TemplateView):

    def get_context_data(self, **kwargs):
        from django.contrib.admin.models import LogEntry

        context = super(AdminHistoryView, self).get_context_data(**kwargs)

        action_list = LogEntry.objects.filter(
            object_id = self.object_id,
            content_type__id__exact = ContentType.objects.get_for_model(self.model).id
        ).select_related().order_by('action_time')
        # If no history was found, see whether this object even exists.
        obj = get_object_or_404(self.model, pk=unquote(self.object_id))

        context.update({
            'title': _('Change history: %s') % force_unicode(obj),
            'action_list': action_list,
            'module_name': capfirst(force_unicode(self.model_opts.verbose_name_plural)),
            'object': obj,
            'app_label': self.model_opts.app_label,
            'opts': self.model_opts,
        })

        context.update(self.extra_context or {})
        return context

    def get_template_names(self):
        form_template = self.admin_opts.object_history_template
        if form_template:
            return [form_template]
        else:
            return [
                "admin/%s/%s/object_history.html" % (self.model_opts.app_label, self.model_opts.object_name.lower()),
                "admin/%s/object_history.html" % self.model_opts.app_label,
                "admin/object_history.html"
            ]
