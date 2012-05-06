from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http import Http404
from django.utils.encoding import force_unicode
from django.views.generic.edit import UpdateView
from django.utils.translation import ugettext as _
from django.contrib.admin.util import unquote
from django.forms.formsets import all_valid
from django.contrib.admin import helpers
from django.utils.html import escape
from django.db import transaction
from django.views.decorators.csrf import csrf_protect
from django.utils.decorators import method_decorator

csrf_protect_m = method_decorator(csrf_protect)

class AdminViewMixin(object):

    def __init__(self, **kwargs):
        super(AdminViewMixin, self).__init__(**kwargs)
        self.model = self.admin_opts.model
        self.model_opts = self.model._meta

    def get_queryset(self):
        return self.admin_opts.queryset(self.request)


class AdminChangeView(AdminViewMixin, UpdateView):

    @csrf_protect_m
    @transaction.commit_on_success
    def dispatch(self, request, *args, **kwargs):
        if request.method == 'POST' and "_saveasnew" in request.POST:
            return self.admin_opts.add_view(request, form_url=reverse('admin:%s_%s_add' %
                                    (self.model_opts.app_label, self.model_opts.module_name),
                                    current_app=self.admin_opts.admin_site.name))

        self.formsets = []
        self.inline_instances = self.admin_opts.get_inline_instances(request)
        return super(AdminChangeView, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        prefixes = {}
        for FormSet, inline in zip(self.admin_opts.get_formsets(self.request, self.object), self.inline_instances):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(instance=self.object, prefix=prefix,
                              queryset=inline.queryset(request))
            self.formsets.append(formset)
        return super(UpdateView, self).get(request, *args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        return self.admin_opts.render_change_form(self.request, context, change=True, obj=self.object, form_url=self.form_url)

    def get_object(self, queryset=None):
        obj = self.admin_opts.get_object(self.request, unquote(self.object_id), queryset=queryset)

        if not self.admin_opts.has_change_permission(self.request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {'name': force_unicode(self.model_opts.verbose_name), 'key': escape(self.object_id)})
        return obj

    def _post_form_validation(self):
        prefixes = {}
        for FormSet, inline in zip(self.admin_opts.get_formsets(self.request, self.object), self.inline_instances):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(self.request.POST, self.request.FILES,
                              instance=self.object, prefix=prefix,
                              queryset=inline.queryset(self.request))
            self.formsets.append(formset)

    def form_valid(self, form):
        self.object = self.admin_opts.save_form(self.request, form, change=True)
        self._post_form_validation()
        if all_valid(self.formsets):
            self.admin_opts.save_model(self.request, self.object, form, True)
            self.admin_opts.save_related(self.request, form, self.formsets, True)
            change_message = self.admin_opts.construct_change_message(self.request, form, self.formsets)
            self.admin_opts.log_change(self.request, self.object, change_message)
            return self.admin_opts.response_change(self.request, self.object)
        return self.render_to_response(self.get_context_data(form=form))

    def form_invalid(self, form):
        self._post_form_validation()
        return self.render_to_response(self.get_context_data(form=form))

    def get_form_class(self):
        return self.admin_opts.get_form(self.request, self.object)

    def get_context_data(self, **kwargs):
        context = super(AdminChangeView, self).get_context_data(**kwargs)

        adminform = helpers.AdminForm(kwargs['form'], self.admin_opts.get_fieldsets(self.request, self.object),
            self.admin_opts.get_prepopulated_fields(self.request, self.object),
            self.admin_opts.get_readonly_fields(self.request, self.object),
            model_admin=self.admin_opts)
        media = self.admin_opts.media + adminform.media

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, self.formsets):
            fieldsets = list(inline.get_fieldsets(self.request, self.object))
            readonly = list(inline.get_readonly_fields(self.request, self.object))
            prepopulated = dict(inline.get_prepopulated_fields(self.request, self.object))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, prepopulated, readonly, model_admin=self)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media

        context.update({
            'title': _('Change %s') % force_unicode(self.model_opts.verbose_name),
            'object_id': self.object_id,
            'is_popup': "_popup" in self.request.REQUEST,
            'adminform': adminform,
            'original': self.object,
            'media': media,
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(adminform.form, self.formsets),
            'app_label': self.model_opts.app_label,
        })
        context.update(self.extra_context or {})
        return context

    def get_template_names(self):
        form_template = self.admin_opts.change_form_template
        if form_template:
            return [form_template]
        else:
            return [
                "admin/%s/%s/change_form.html" % (self.model_opts.app_label, self.model_opts.object_name.lower()),
                "admin/%s/change_form.html" % self.model_opts.app_label,
                "admin/change_form.html"
            ]