import operator
from functools import reduce

from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.utils.encoding import force_unicode
from django.views.generic.base import TemplateView
from django.utils.translation import ugettext as _, ungettext
from django.template.response import SimpleTemplateResponse
from django.contrib.admin import helpers
from django.contrib.admin.views.base import AdminViewMixin
from django.contrib.admin.util import (quote, get_fields_from_path,
    lookup_needs_distinct, prepare_lookup_value)
from django.core.exceptions import SuspiciousOperation, ImproperlyConfigured
from django.core.paginator import InvalidPage
from django.db import models
from django.db.models.fields import FieldDoesNotExist
from django.utils.datastructures import SortedDict
from django.utils.encoding import smart_str
from django.utils.translation import ugettext, ugettext_lazy
from django.utils.http import urlencode


# Changelist settings
ALL_VAR = 'all'
ORDER_VAR = 'o'
ORDER_TYPE_VAR = 'ot'
PAGE_VAR = 'p'
SEARCH_VAR = 'q'
TO_FIELD_VAR = 't'
IS_POPUP_VAR = 'pop'
ERROR_FLAG = 'e'

IGNORED_PARAMS = (
    ALL_VAR, ORDER_VAR, ORDER_TYPE_VAR, SEARCH_VAR, IS_POPUP_VAR, TO_FIELD_VAR)

# Text to display within change-list table cells if the value is blank.
EMPTY_CHANGELIST_VALUE = ugettext_lazy('(None)')


class ChangeList(object):
    def __init__(self, request, model, list_display, list_display_links,
            list_filter, date_hierarchy, search_fields, list_select_related,
            list_per_page, list_max_show_all, list_editable, model_admin):
        self.model = model
        self.opts = model._meta
        self.lookup_opts = self.opts
        self.root_query_set = model_admin.queryset(request)
        self.list_display = list_display
        self.list_display_links = list_display_links
        self.list_filter = list_filter
        self.date_hierarchy = date_hierarchy
        self.search_fields = search_fields
        self.list_select_related = list_select_related
        self.list_per_page = list_per_page
        self.list_max_show_all = list_max_show_all
        self.model_admin = model_admin

        # Get search parameters from the query string.
        try:
            self.page_num = int(request.GET.get(PAGE_VAR, 0))
        except ValueError:
            self.page_num = 0
        self.show_all = ALL_VAR in request.GET
        self.is_popup = IS_POPUP_VAR in request.GET
        self.to_field = request.GET.get(TO_FIELD_VAR)
        self.params = dict(request.GET.items())
        if PAGE_VAR in self.params:
            del self.params[PAGE_VAR]
        if ERROR_FLAG in self.params:
            del self.params[ERROR_FLAG]

        if self.is_popup:
            self.list_editable = ()
        else:
            self.list_editable = list_editable
        self.query = request.GET.get(SEARCH_VAR, '')
        self.query_set = self.get_query_set(request)
        self.get_results(request)
        if self.is_popup:
            title = ugettext('Select %s')
        else:
            title = ugettext('Select %s to change')
        self.title = title % force_unicode(self.opts.verbose_name)
        self.pk_attname = self.lookup_opts.pk.attname

    def get_filters(self, request):
        from django.contrib.admin import FieldListFilter
        from django.contrib.admin.options import IncorrectLookupParameters

        lookup_params = self.params.copy() # a dictionary of the query string
        use_distinct = False

        # Remove all the parameters that are globally and systematically
        # ignored.
        for ignored in IGNORED_PARAMS:
            if ignored in lookup_params:
                del lookup_params[ignored]

        # Normalize the types of keys
        for key, value in lookup_params.items():
            if not isinstance(key, str):
                # 'key' will be used as a keyword argument later, so Python
                # requires it to be a string.
                del lookup_params[key]
                lookup_params[smart_str(key)] = value

            if not self.model_admin.lookup_allowed(key, value):
                raise SuspiciousOperation("Filtering by %s not allowed" % key)

        filter_specs = []
        if self.list_filter:
            for list_filter in self.list_filter:
                if callable(list_filter):
                    # This is simply a custom list filter class.
                    spec = list_filter(request, lookup_params,
                        self.model, self.model_admin)
                else:
                    field_path = None
                    if isinstance(list_filter, (tuple, list)):
                        # This is a custom FieldListFilter class for a given field.
                        field, field_list_filter_class = list_filter
                    else:
                        # This is simply a field name, so use the default
                        # FieldListFilter class that has been registered for
                        # the type of the given field.
                        field, field_list_filter_class = list_filter, FieldListFilter.create
                    if not isinstance(field, models.Field):
                        field_path = field
                        field = get_fields_from_path(self.model, field_path)[-1]
                    spec = field_list_filter_class(field, request, lookup_params,
                        self.model, self.model_admin, field_path=field_path)
                    # Check if we need to use distinct()
                    use_distinct = (use_distinct or
                                    lookup_needs_distinct(self.lookup_opts,
                                                          field_path))
                if spec and spec.has_output():
                    filter_specs.append(spec)

        # At this point, all the parameters used by the various ListFilters
        # have been removed from lookup_params, which now only contains other
        # parameters passed via the query string. We now loop through the
        # remaining parameters both to ensure that all the parameters are valid
        # fields and to determine if at least one of them needs distinct(). If
        # the lookup parameters aren't real fields, then bail out.
        try:
            for key, value in lookup_params.items():
                lookup_params[key] = prepare_lookup_value(key, value)
                use_distinct = (use_distinct or
                                lookup_needs_distinct(self.lookup_opts, key))
            return filter_specs, bool(filter_specs), lookup_params, use_distinct
        except FieldDoesNotExist as e:
            raise IncorrectLookupParameters(e)

    def get_query_string(self, new_params=None, remove=None):
        if new_params is None: new_params = {}
        if remove is None: remove = []
        p = self.params.copy()
        for r in remove:
            for k in p.keys():
                if k.startswith(r):
                    del p[k]
        for k, v in new_params.items():
            if v is None:
                if k in p:
                    del p[k]
            else:
                p[k] = v
        return '?%s' % urlencode(p)

    def get_results(self, request):
        from django.contrib.admin.options import IncorrectLookupParameters

        paginator = self.model_admin.get_paginator(request, self.query_set, self.list_per_page)
        # Get the number of objects, with admin filters applied.
        result_count = paginator.count

        # Get the total number of objects, with no admin filters applied.
        # Perform a slight optimization: Check to see whether any filters were
        # given. If not, use paginator.hits to calculate the number of objects,
        # because we've already done paginator.hits and the value is cached.
        if not self.query_set.query.where:
            full_result_count = result_count
        else:
            full_result_count = self.root_query_set.count()

        can_show_all = result_count <= self.list_max_show_all
        multi_page = result_count > self.list_per_page

        # Get the list of objects to display on this page.
        if (self.show_all and can_show_all) or not multi_page:
            result_list = self.query_set._clone()
        else:
            try:
                result_list = paginator.page(self.page_num+1).object_list
            except InvalidPage:
                raise IncorrectLookupParameters

        self.result_count = result_count
        self.full_result_count = full_result_count
        self.result_list = result_list
        self.can_show_all = can_show_all
        self.multi_page = multi_page
        self.paginator = paginator

    def _get_default_ordering(self):
        ordering = []
        if self.model_admin.ordering:
            ordering = self.model_admin.ordering
        elif self.lookup_opts.ordering:
            ordering = self.lookup_opts.ordering
        return ordering

    def get_ordering_field(self, field_name):
        """
        Returns the proper model field name corresponding to the given
        field_name to use for ordering. field_name may either be the name of a
        proper model field or the name of a method (on the admin or model) or a
        callable with the 'admin_order_field' attribute. Returns None if no
        proper model field name can be matched.
        """
        try:
            field = self.lookup_opts.get_field(field_name)
            return field.name
        except models.FieldDoesNotExist:
            # See whether field_name is a name of a non-field
            # that allows sorting.
            if callable(field_name):
                attr = field_name
            elif hasattr(self.model_admin, field_name):
                attr = getattr(self.model_admin, field_name)
            else:
                attr = getattr(self.model, field_name)
            return getattr(attr, 'admin_order_field', None)

    def get_ordering(self, request, queryset):
        """
        Returns the list of ordering fields for the change list.
        First we check the get_ordering() method in model admin, then we check
        the object's default ordering. Then, any manually-specified ordering
        from the query string overrides anything. Finally, a deterministic
        order is guaranteed by ensuring the primary key is used as the last
        ordering field.
        """
        params = self.params
        ordering = list(self.model_admin.get_ordering(request)
                        or self._get_default_ordering())
        if ORDER_VAR in params:
            # Clear ordering and used params
            ordering = []
            order_params = params[ORDER_VAR].split('.')
            for p in order_params:
                try:
                    none, pfx, idx = p.rpartition('-')
                    field_name = self.list_display[int(idx)]
                    order_field = self.get_ordering_field(field_name)
                    if not order_field:
                        continue # No 'admin_order_field', skip it
                    ordering.append(pfx + order_field)
                except (IndexError, ValueError):
                    continue # Invalid ordering specified, skip it.

        # Add the given query's ordering fields, if any.
        ordering.extend(queryset.query.order_by)

        # Ensure that the primary key is systematically present in the list of
        # ordering fields so we can guarantee a deterministic order across all
        # database backends.
        pk_name = self.lookup_opts.pk.name
        if not (set(ordering) & set(['pk', '-pk', pk_name, '-' + pk_name])):
            # The two sets do not intersect, meaning the pk isn't present. So
            # we add it.
            ordering.append('-pk')

        return ordering

    def get_ordering_field_columns(self):
        """
        Returns a SortedDict of ordering field column numbers and asc/desc
        """

        # We must cope with more than one column having the same underlying sort
        # field, so we base things on column numbers.
        ordering = self._get_default_ordering()
        ordering_fields = SortedDict()
        if ORDER_VAR not in self.params:
            # for ordering specified on ModelAdmin or model Meta, we don't know
            # the right column numbers absolutely, because there might be more
            # than one column associated with that ordering, so we guess.
            for field in ordering:
                if field.startswith('-'):
                    field = field[1:]
                    order_type = 'desc'
                else:
                    order_type = 'asc'
                for index, attr in enumerate(self.list_display):
                    if self.get_ordering_field(attr) == field:
                        ordering_fields[index] = order_type
                        break
        else:
            for p in self.params[ORDER_VAR].split('.'):
                none, pfx, idx = p.rpartition('-')
                try:
                    idx = int(idx)
                except ValueError:
                    continue # skip it
                ordering_fields[idx] = 'desc' if pfx == '-' else 'asc'
        return ordering_fields

    def get_query_set(self, request):
        from django.contrib.admin.options import IncorrectLookupParameters

        # First, we collect all the declared list filters.
        (self.filter_specs, self.has_filters, remaining_lookup_params,
         use_distinct) = self.get_filters(request)

        # Then, we let every list filter modify the queryset to its liking.
        qs = self.root_query_set
        for filter_spec in self.filter_specs:
            new_qs = filter_spec.queryset(request, qs)
            if new_qs is not None:
                qs = new_qs

        try:
            # Finally, we apply the remaining lookup parameters from the query
            # string (i.e. those that haven't already been processed by the
            # filters).
            qs = qs.filter(**remaining_lookup_params)
        except (SuspiciousOperation, ImproperlyConfigured):
            # Allow certain types of errors to be re-raised as-is so that the
            # caller can treat them in a special way.
            raise
        except Exception as e:
            # Every other error is caught with a naked except, because we don't
            # have any other way of validating lookup parameters. They might be
            # invalid if the keyword arguments are incorrect, or if the values
            # are not in the correct type, so we might get FieldError,
            # ValueError, ValidationError, or ?.
            raise IncorrectLookupParameters(e)

        # Use select_related() if one of the list_display options is a field
        # with a relationship and the provided queryset doesn't already have
        # select_related defined.
        if not qs.query.select_related:
            if self.list_select_related:
                qs = qs.select_related()
            else:
                for field_name in self.list_display:
                    try:
                        field = self.lookup_opts.get_field(field_name)
                    except models.FieldDoesNotExist:
                        pass
                    else:
                        if isinstance(field.rel, models.ManyToOneRel):
                            qs = qs.select_related()
                            break

        # Set ordering.
        ordering = self.get_ordering(request, qs)
        qs = qs.order_by(*ordering)

        # Apply keyword searches.
        def construct_search(field_name):
            if field_name.startswith('^'):
                return "%s__istartswith" % field_name[1:]
            elif field_name.startswith('='):
                return "%s__iexact" % field_name[1:]
            elif field_name.startswith('@'):
                return "%s__search" % field_name[1:]
            else:
                return "%s__icontains" % field_name

        if self.search_fields and self.query:
            orm_lookups = [construct_search(str(search_field))
                           for search_field in self.search_fields]
            for bit in self.query.split():
                or_queries = [models.Q(**{orm_lookup: bit})
                              for orm_lookup in orm_lookups]
                qs = qs.filter(reduce(operator.or_, or_queries))
            if not use_distinct:
                for search_spec in orm_lookups:
                    if lookup_needs_distinct(self.lookup_opts, search_spec):
                        use_distinct = True
                        break

        if use_distinct:
            return qs.distinct()
        else:
            return qs

    def url_for_result(self, result):
        return "%s/" % quote(getattr(result, self.pk_attname))


class AdminChangeListView(AdminViewMixin, TemplateView):

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

        return super(AdminChangeListView, self).dispatch(request, *args, **kwargs)

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
        context = super(AdminChangeListView, self).get_context_data(**kwargs)

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

