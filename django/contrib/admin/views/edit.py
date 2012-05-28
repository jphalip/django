from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http import Http404, HttpResponseRedirect
from django.utils.encoding import force_unicode
from django.utils.translation import ugettext as _
from django.views.generic.edit import UpdateView, CreateView, DeleteView
from django.contrib.admin.util import unquote, get_deleted_objects
from django.contrib.admin.views.base import AdminViewMixin
from django.forms.formsets import all_valid
from django.contrib.admin import helpers
from django.utils.html import escape
from django.db import models, router


class FormSetsMixin(object):
    def construct_formsets(self, **kwargs):
        """
        Constructs the formsets taking care of any clashing prefixes.

        It accepts kwargs for the FormSet instantiating and adds the POST and
        FILES if available.
        """

        prefixes = {}
        # Check if we have an instance or if we are creating a new one
        object = getattr(self, 'object', None)

        for FormSet, inline in zip(self.admin_opts.get_formsets(
            self.request, object), self.inline_instances):

            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])

            formset_kwargs = {
                'prefix': prefix,
                'queryset': inline.queryset(self.request),
                'instance': object or self.model()
            }

            if self.request.method in ('POST', 'PUT'):
                formset_kwargs.update({
                    'data': self.request.POST,
                    'files': self.request.FILES,
                })

            formset_kwargs.update(kwargs)

            self.formsets.append(FormSet(**formset_kwargs))


class AdminDeleteView(AdminViewMixin, DeleteView):

    def dispatch(self, request, *args, **kwargs):
        return super(AdminDeleteView, self).dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        object = self.admin_opts.get_object(
            self.request, unquote(self.object_id), queryset=queryset)
        if not self.admin_opts.has_delete_permission(self.request, object):
            raise PermissionDenied
        if object is None:
            raise Http404(
                _('%(name)s object with primary key %(key)r does not exist.') % {
                    'name': force_unicode(self.model_opts.verbose_name),
                    'key': escape(self.object_id)})
        using = router.db_for_write(self.model)
        # Populate deleted_objects, a data structure of all related objects that
        # will also be deleted.
        (self.deleted_objects, self.perms_needed, self.protected) = get_deleted_objects(
            [object], self.model_opts, self.request.user, self.admin_opts.admin_site, using)
        return object

    def post(self, *args, **kwargs):
        self.object = self.get_object()
        # The user has already confirmed the deletion.
        if self.perms_needed:
            raise PermissionDenied
        obj_display = force_unicode(self.object)
        self.admin_opts.log_deletion(self.request, self.object, obj_display)
        self.admin_opts.delete_model(self.request, self.object)

        self.admin_opts.message_user(
            self.request, _('The %(name)s "%(obj)s" was deleted successfully.') % {
                'name': force_unicode(self.model_opts.verbose_name),
                'obj': force_unicode(obj_display)})

        if not self.admin_opts.has_change_permission(self.request, None):
            return HttpResponseRedirect(
                reverse('admin:index', current_app=self.admin_opts.admin_site.name))
        return HttpResponseRedirect(
            reverse('admin:%s_%s_changelist' % (
                self.model_opts.app_label, self.model_opts.module_name),
                current_app=self.admin_opts.admin_site.name))

    def get_context_data(self, **kwargs):
        context = super(AdminDeleteView, self).get_context_data(**kwargs)

        object_name = force_unicode(self.model_opts.verbose_name)

        if self.perms_needed or self.protected:
            title = _("Cannot delete %(name)s") % {"name": object_name}
        else:
            title = _("Are you sure?")

        context.update({
            "title": title,
            "object_name": object_name,
            "object": self.object,
            "deleted_objects": self.deleted_objects,
            "perms_lacking": self.perms_needed,
            "protected": self.protected,
            "opts": self.model_opts,
            "app_label": self.model_opts.app_label,
        })

        context.update(self.extra_context or {})
        return context

    def get_template_names(self):
        form_template = self.admin_opts.delete_confirmation_template
        if form_template:
            return [form_template]
        else:
            return [
                "admin/%s/%s/delete_confirmation.html" % (self.model_opts.app_label, self.model_opts.object_name.lower()),
                "admin/%s/delete_confirmation.html" % self.model_opts.app_label,
                "admin/delete_confirmation.html"
            ]


class AdminAddView(AdminViewMixin, FormSetsMixin, CreateView):

    def dispatch(self, request, *args, **kwargs):
        if not self.admin_opts.has_add_permission(request):
            raise PermissionDenied

        self.formsets = []
        self.inline_instances = self.admin_opts.get_inline_instances(request)
        return super(AdminAddView, self).dispatch(request, *args, **kwargs)

    def get_form_class(self):
        return self.admin_opts.get_form(self.request, self.object)

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

    def form_valid(self, form):
        self.object = self.admin_opts.save_form(self.request, form, change=False)
        self.construct_formsets(save_as_new="_saveasnew" in self.request.POST)
        if all_valid(self.formsets):
            self.admin_opts.save_model(self.request, self.object, form, False)
            self.admin_opts.save_related(self.request, form, self.formsets, False)
            self.admin_opts.log_addition(self.request, self.object)
            return self.admin_opts.response_add(self.request, self.object)
        return self.render_to_response(self.get_context_data(form=form))

    def form_invalid(self, form):
        self.object = self.model()
        self.construct_formsets(save_as_new="_saveasnew" in self.request.POST)
        return self.render_to_response(self.get_context_data(form=form))

    def get_form_kwargs(self):
        kwargs = super(AdminAddView, self).get_form_kwargs()

        # Prepare the dict of initial data from the request.
        # We have to special-case M2Ms as a list of comma-separated PKs.
        initial = dict(self.request.GET.items())
        for k in initial:
            try:
                f = self.model_opts.get_field(k)
            except models.FieldDoesNotExist:
                continue
            if isinstance(f, models.ManyToManyField):
                initial[k] = initial[k].split(",")
        kwargs.update({'initial': initial})
        return kwargs

    def render_to_response(self, context, **response_kwargs):
        return self.admin_opts.render_change_form(
            self.request, context, add=True, obj=self.object,
            form_url=self.form_url)

    def get(self, request, *args, **kwargs):
        self.construct_formsets()
        return super(CreateView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(AdminAddView, self).get_context_data(**kwargs)

        adminForm = helpers.AdminForm(
            kwargs['form'], list(self.admin_opts.get_fieldsets(self.request)),
            self.admin_opts.get_prepopulated_fields(self.request),
            self.admin_opts.get_readonly_fields(self.request),
            model_admin=self.admin_opts)
        media = self.admin_opts.media + adminForm.media

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, self.formsets):
            fieldsets = list(inline.get_fieldsets(self.request))
            readonly = list(inline.get_readonly_fields(self.request))
            prepopulated = dict(inline.get_prepopulated_fields(self.request))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, prepopulated, readonly, model_admin=self.admin_opts)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media

        context.update({
            'title': _('Add %s') % force_unicode(self.model_opts.verbose_name),
            'adminform': adminForm,
            'is_popup': "_popup" in self.request.REQUEST,
            'show_delete': False,
            'media': media,
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(adminForm.form, self.formsets),
            'app_label': self.model_opts.app_label,
        })
        context.update(self.extra_context or {})
        return context


class AdminChangeView(AdminViewMixin, FormSetsMixin, UpdateView):

    def dispatch(self, request, *args, **kwargs):
        if request.method == 'POST' and "_saveasnew" in request.POST:
            return self.admin_opts.add_view(
                request, form_url=reverse('admin:%s_%s_add' %
                                    (self.model_opts.app_label, self.model_opts.module_name),
                                    current_app=self.admin_opts.admin_site.name))

        self.formsets = []
        self.inline_instances = self.admin_opts.get_inline_instances(request)
        return super(AdminChangeView, self).dispatch(request, *args, **kwargs)

    def get_form_class(self):
        return self.admin_opts.get_form(self.request, self.object)

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

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.construct_formsets()
        return super(UpdateView, self).get(request, *args, **kwargs)

    def get_object(self, queryset=None):
        obj = self.admin_opts.get_object(self.request, unquote(self.object_id), queryset=queryset)

        if not self.admin_opts.has_change_permission(self.request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(
                _('%(name)s object with primary key %(key)r does not exist.') % {
                    'name': force_unicode(self.model_opts.verbose_name),
                    'key': escape(self.object_id)})
        return obj

    def render_to_response(self, context, **response_kwargs):
        return self.admin_opts.render_change_form(
            self.request, context, change=True, obj=self.object,
            form_url=self.form_url)

    def form_valid(self, form):
        self.object = self.admin_opts.save_form(self.request, form, change=True)
        self.construct_formsets()
        if all_valid(self.formsets):
            self.admin_opts.save_model(self.request, self.object, form, True)
            self.admin_opts.save_related(self.request, form, self.formsets, True)
            change_message = self.admin_opts.construct_change_message(self.request, form, self.formsets)
            self.admin_opts.log_change(self.request, self.object, change_message)
            return self.admin_opts.response_change(self.request, self.object)
        return self.render_to_response(self.get_context_data(form=form))

    def form_invalid(self, form):
        self.construct_formsets()
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super(AdminChangeView, self).get_context_data(**kwargs)

        adminform = helpers.AdminForm(
            kwargs['form'], self.admin_opts.get_fieldsets(self.request, self.object),
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
                fieldsets, prepopulated, readonly, model_admin=self.admin_opts)
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
