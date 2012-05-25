from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http import Http404, HttpResponseRedirect
from django.utils.encoding import force_unicode
from django.views.generic.base import TemplateView
from django.views.generic.edit import UpdateView, CreateView, DeleteView
from django.utils.translation import ugettext as _, ungettext
from django.contrib.admin.util import unquote, get_deleted_objects
from django.forms.formsets import all_valid
from django.contrib.admin import helpers
from django.utils.html import escape
from django.db import transaction, models, router
from django.views.decorators.csrf import csrf_protect
from django.utils.decorators import method_decorator
from django.contrib.admin.views.main import ERROR_FLAG
from django.template.response import SimpleTemplateResponse


csrf_protect_m = method_decorator(csrf_protect)


class AdminViewMixin(object):

    def __init__(self, **kwargs):
        super(AdminViewMixin, self).__init__(**kwargs)
        self.model = self.admin_opts.model
        self.model_opts = self.model._meta

    def get_queryset(self):
        return self.admin_opts.queryset(self.request)


class ChangeListView(AdminViewMixin, TemplateView):

    @csrf_protect_m
    @transaction.commit_on_success
    def dispatch(self, request, *args, **kwargs):
        from django.contrib.admin.options import IncorrectLookupParameters

        if not self.admin_opts.has_change_permission(request, None):
            raise PermissionDenied

        list_display = self.admin_opts.get_list_display(request)
        list_display_links = self.admin_opts.get_list_display_links(request, list_display)

        # Check actions to see if any are available on this changelist
        self.actions = self.admin_opts.get_actions(request)
        if self.actions:
            # Add the action checkboxes if there are any actions available.
            list_display = ['action_checkbox'] +  list(list_display)

        ChangeList = self.admin_opts.get_changelist(request)
        try:
            self.changelist = ChangeList(request, self.admin_opts.model, list_display,
                list_display_links, self.admin_opts.list_filter, self.admin_opts.date_hierarchy,
                self.admin_opts.search_fields, self.admin_opts.list_select_related,
                self.admin_opts.list_per_page, self.admin_opts.list_max_show_all, self.admin_opts.list_editable,
                self.admin_opts)
        except IncorrectLookupParameters:
            # Wacky lookup parameters were given, so redirect to the main
            # changelist page, without parameters, and pass an 'invalid=1'
            # parameter via the query string. If wacky parameters were given
            # and the 'invalid=1' parameter was already in the query string,
            # something is screwed up with the database, so display an error
            # page.
            if ERROR_FLAG in request.GET.keys():
                return SimpleTemplateResponse('admin/invalid_setup.html', {
                    'title': _('Database error'),
                })
            return HttpResponseRedirect(request.path + '?' + ERROR_FLAG + '=1')

        # If we're allowing changelist editing, we need to construct a formset
        # for the changelist given all the fields to be edited. Then we'll
        # use the formset to validate/process POSTed data.
        self.formset = self.changelist.formset = None

        return super(ChangeListView, self).dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # If the request was POSTed, this might be a bulk action or a bulk
        # edit. Try to look up an action or confirmation first, but if this
        # isn't an action the POST will fall through to the bulk edit check,
        # below.
        action_failed = False
        selected = request.POST.getlist(helpers.ACTION_CHECKBOX_NAME)

        # Actions with no confirmation
        if (self.actions and request.method == 'POST' and
                'index' in request.POST and '_save' not in request.POST):
            if selected:
                response = self.admin_opts.response_action(request, queryset=self.changelist.get_query_set(request))
                if response:
                    return response
                else:
                    action_failed = True
            else:
                msg = _("Items must be selected in order to perform "
                        "actions on them. No items have been changed.")
                self.admin_opts.message_user(request, msg)
                action_failed = True

        # Actions with confirmation
        if (self.actions and
            helpers.ACTION_CHECKBOX_NAME in request.POST and
            'index' not in request.POST and
            '_save' not in request.POST and
            selected):
                response = self.admin_opts.response_action(request, queryset=self.changelist.get_query_set(request))
                if response:
                    return response
                else:
                    action_failed = True

        # Handle POSTed bulk-edit data.
        if (self.changelist.list_editable and
            '_save' in request.POST and not action_failed):
            FormSet = self.admin_opts.get_changelist_formset(request)
            self.formset = self.changelist.formset = FormSet(request.POST, request.FILES, queryset=self.changelist.result_list)
            if self.formset.is_valid():
                changecount = 0
                for form in self.formset.forms:
                    if form.has_changed():
                        obj = self.admin_opts.save_form(request, form, change=True)
                        self.admin_opts.save_model(request, obj, form, change=True)
                        self.admin_opts.save_related(request, form, formsets=[], change=True)
                        change_msg = self.admin_opts.construct_change_message(request, form, None)
                        self.admin_opts.log_change(request, obj, change_msg)
                        changecount += 1

                if changecount:
                    if changecount == 1:
                        name = force_unicode(self.model_opts.verbose_name)
                    else:
                        name = force_unicode(self.model_opts.verbose_name_plural)
                    msg = ungettext("%(count)s %(name)s was changed successfully.",
                                    "%(count)s %(name)s were changed successfully.",
                                    changecount) % {'count': changecount,
                                                    'name': name,
                                                    'obj': force_unicode(obj)}
                    self.admin_opts.message_user(request, msg)

                return HttpResponseRedirect(request.get_full_path())

        return self.render_to_response(self.get_context_data(), current_app=self.admin_opts.admin_site.name)

    def get(self, request, *args, **kwargs):
        # Handle GET -- construct a formset for display.
        if self.changelist.list_editable:
            FormSet = self.admin_opts.get_changelist_formset(request)
            self.formset = self.changelist.formset = FormSet(queryset=self.changelist.result_list)
        return self.render_to_response(self.get_context_data(), current_app=self.admin_opts.admin_site.name)

    def get_template_names(self):
        form_template = self.admin_opts.change_list_template
        if form_template:
            return [form_template]
        else:
            return [
                "admin/%s/%s/change_list.html" % (self.model_opts.app_label, self.model_opts.object_name.lower()),
                "admin/%s/change_list.html" % self.model_opts.app_label,
                "admin/change_list.html"
            ]

    def get_context_data(self, **kwargs):
        context = super(ChangeListView, self).get_context_data(**kwargs)

        # Build the list of media to be used by the formset.
        if self.formset:
            media = self.admin_opts.media + self.formset.media
        else:
            media = self.admin_opts.media

        # Build the action form and populate it with available actions.
        if self.actions:
            self.action_form = self.admin_opts.action_form(auto_id=None)
            self.action_form.fields['action'].choices = self.admin_opts.get_action_choices(self.request)
        else:
            self.action_form = None

        selection_note_all = ungettext('%(total_count)s selected',
            'All %(total_count)s selected', self.changelist.result_count)

        context.update({
            'module_name': force_unicode(self.model_opts.verbose_name_plural),
            'selection_note': _('0 of %(cnt)s selected') % {'cnt': len(self.changelist.result_list)},
            'selection_note_all': selection_note_all % {'total_count': self.changelist.result_count},
            'title': self.changelist.title,
            'is_popup': self.changelist.is_popup,
            'cl': self.changelist,
            'media': media,
            'has_add_permission': self.admin_opts.has_add_permission(self.request),
            'app_label': self.model_opts.app_label,
            'action_form': self.action_form,
            'actions_on_top': self.admin_opts.actions_on_top,
            'actions_on_bottom': self.admin_opts.actions_on_bottom,
            'actions_selection_counter': self.admin_opts.actions_selection_counter,
        })

        context.update(self.extra_context or {})
        return context



class AdminDeleteView(AdminViewMixin, DeleteView):

    @csrf_protect_m
    @transaction.commit_on_success
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


class AdminAddView(AdminViewMixin, CreateView):

    @csrf_protect_m
    @transaction.commit_on_success
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
        self._post_form_validation()
        if all_valid(self.formsets):
            self.admin_opts.save_model(self.request, self.object, form, False)
            self.admin_opts.save_related(self.request, form, self.formsets, False)
            self.admin_opts.log_addition(self.request, self.object)
            return self.admin_opts.response_add(self.request, self.object)
        return self.render_to_response(self.get_context_data(form=form))

    def form_invalid(self, form):
        self.object = self.model()
        self._post_form_validation()
        return self.render_to_response(self.get_context_data(form=form))

    def _post_form_validation(self):
        prefixes = {}
        for FormSet, inline in zip(self.admin_opts.get_formsets(self.request), self.inline_instances):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(data=self.request.POST, files=self.request.FILES,
                              instance=self.object,
                              save_as_new="_saveasnew" in self.request.POST,
                              prefix=prefix, queryset=inline.queryset(self.request))
            self.formsets.append(formset)

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
        prefixes = {}
        for FormSet, inline in zip(self.admin_opts.get_formsets(request), self.inline_instances):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset = FormSet(instance=self.model(), prefix=prefix,
                              queryset=inline.queryset(request))
            self.formsets.append(formset)
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



class AdminChangeView(AdminViewMixin, UpdateView):

    @csrf_protect_m
    @transaction.commit_on_success
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
