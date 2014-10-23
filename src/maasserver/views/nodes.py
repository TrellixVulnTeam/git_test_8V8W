# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Nodes views."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    'enlist_preseed_view',
    'MacAdd',
    'MacDelete',
    'NodeDelete',
    'NodeEventListView',
    'NodeListView',
    'NodePreseedView',
    'NodeView',
    'NodeEdit',
    'prefetch_nodes_listing',
    ]

from cgi import escape
import logging
from operator import attrgetter
import re
from textwrap import dedent
from urllib import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.http import QueryDict
from django.shortcuts import (
    get_object_or_404,
    render_to_response,
    )
from django.template import RequestContext
from django.utils.safestring import mark_safe
from django.views.generic import (
    CreateView,
    DetailView,
    UpdateView,
    )
from django.views.generic.edit import (
    FormMixin,
    ProcessFormView,
    )
from lxml import etree
from maasserver.clusterrpc.power_parameters import get_power_types
from maasserver.enum import (
    NODE_BOOT,
    NODE_PERMISSION,
    NODE_STATUS,
    )
from maasserver.exceptions import MAASAPIException
from maasserver.forms import (
    BulkNodeActionForm,
    get_action_form,
    get_node_edit_form,
    MACAddressForm,
    SetZoneBulkAction,
    )
from maasserver.models import (
    MACAddress,
    Node,
    StaticIPAddress,
    Tag,
    )
from maasserver.models.config import Config
from maasserver.models.event import Event
from maasserver.models.nodeprobeddetails import get_single_probed_details
from maasserver.node_action import ACTIONS_DICT
from maasserver.node_constraint_filter_forms import (
    AcquireNodeForm,
    JUJU_ACQUIRE_FORM_FIELDS_MAPPING,
    )
from maasserver.preseed import (
    get_enlist_preseed,
    get_preseed,
    OS_WITH_IPv6_SUPPORT,
    )
from maasserver.third_party_drivers import get_third_party_driver
from maasserver.utils.converters import XMLToYAML
from maasserver.views import (
    HelpfulDeleteView,
    PaginatedListView,
    )
from metadataserver.enum import RESULT_TYPE
from metadataserver.models import NodeResult
from netaddr import (
    EUI,
    IPAddress,
    NotRegisteredError,
    )
from provisioningserver.tags import merge_details_cleanly


def _parse_constraints(query_string):
    """Turn query string from user into a QueryDict.

    This method parse the given query string and returns a QueryDict suitable
    to be passed to AcquireNodeForm().
    This is basically to mimic the way the juju behaves: any parameters with
    a value of 'any' will be ignored.
    """
    constraints = []
    for word in query_string.strip().split():
        parts = word.split("=", 1)
        if len(parts) != 2:
            # Empty constraint.
            constraints.append("%s=" % parts[0])
        elif parts[1] != "any":
            # 'any' constraint: discard it.
            constraints.append("%s=%s" % tuple(parts))
    return QueryDict('&'.join(constraints))


def message_from_form_stats(action, done, not_actionable, not_permitted):
    """Return a message suitable for user display from the given stats."""
    action_name = 'The action "%s"' % action.display_bulk
    # singular/plural messages.
    done_templates = [
        '%s was successfully performed on %d node.',
        '%s was successfully performed on %d nodes.'
    ]
    not_actionable_templates = [
        ('%s could not be performed on %d node because its '
         'state does not allow that action.'),
        ('%s could not be performed on %d nodes because their '
         'state does not allow that action.'),
    ]
    not_permitted_templates = [
        ('%s could not be performed on %d node because that '
         "action is not permitted on that node."),
        ('%s could not be performed on %d nodes because that '
         "action is not permitted on these nodes."),
    ]
    number_message = [
        (done, done_templates),
        (not_actionable, not_actionable_templates),
        (not_permitted, not_permitted_templates)]
    message = []
    for number, message_templates in number_message:
        singular, plural = message_templates
        if number != 0:
            message_template = singular if number == 1 else plural
            message.append(message_template % (action_name, number))
            # Override the action name so that only the first sentence will
            # contain the full name of the action.
            action_name = 'It'
    return ' '.join(message)


def prefetch_nodes_listing(nodes_query):
    """Prefetch any data needed to display a given query of nodes.

    :param nodes_query: A query set of nodes.
    :return: A version of `nodes_query` that prefetches any data needed for
        displaying these nodes as a listing.
    """
    return (
        nodes_query
        .prefetch_related('macaddress_set')
        .select_related('nodegroup')
        .prefetch_related('nodegroup__nodegroupinterface_set')
        .prefetch_related('zone'))


def generate_js_power_types(nodegroup=None):
    """Return a JavaScript definition of supported power-type choices.

    Produces an array of power-type identifiers, starting with the opening
    bracket and ending with the closing bracket, without line breaks on either
    end.  Entries are one per line, sorted lexicographically.
    """
    if nodegroup is not None:
        nodegroup = [nodegroup]
    power_types = get_power_types(nodegroup, ignore_errors=True)
    names = ['"%s"' % power_type for power_type in sorted(power_types)]
    return mark_safe("[\n%s\n]" % ',\n'.join(names))


def get_vendor_for_mac(mac):
    """Return vendor for MAC."""
    data = EUI(mac)
    try:
        return data.oui.registration().org
    except NotRegisteredError:
        return 'Unknown Vendor'


def configure_macs(nodes):
    """Configures the each node in the query to have an "macs" attribute,
    that contains a list of macs, sorted by created.

    The list is structed to contain the MAC and its vendor.
    """
    for node in nodes:
        macs = node.macaddress_set.all()
        macs = sorted(macs, key=lambda mac: mac.created)
        macs = ['%s' % mac.mac_address for mac in macs]
        if len(macs) == 0:
            node.primary_mac = None
            node.primary_mac_vendor = None
            node.extra_macs = []
        else:
            node.primary_mac = macs[0]
            node.primary_mac_vendor = get_vendor_for_mac(node.primary_mac)
            node.extra_macs = macs[1:]
    return nodes


class NodeListView(PaginatedListView, FormMixin, ProcessFormView):

    context_object_name = "node_list"
    form_class = BulkNodeActionForm
    sort_fields = (
        'hostname', 'status', 'owner', 'cpu_count',
        'memory', 'storage', 'zone')
    late_sort_fields = ('primary_mac', )

    def populate_modifiers(self, request):
        self.query = request.GET.get("query")
        self.query_error = None
        self.sort_by = request.GET.get("sort")
        self.sort_dir = request.GET.get("dir")

    def get(self, request, *args, **kwargs):
        """Handle a GET request."""
        self.populate_modifiers(request)

        if Config.objects.get_config("enable_third_party_drivers"):
            # Show a notice to all users that third-party drivers are
            # enabled. Administrative users also get a link to the
            # settings page where they can disable this feature.
            notice = construct_third_party_drivers_notice(
                request.user.is_superuser)
            messages.info(request, notice)

        return super(NodeListView, self).get(request, *args, **kwargs)

    def get_preserved_params(self):
        """List of GET parameters that need to be preserved by POST
        requests.

        These are sorting and search option we want a POST request to
        preserve so that the display after a POST request is similar
        to the display before the request."""
        return ["dir", "query", "page", "sort"]

    def get_preserved_query(self):
        params = {
            param: self.request.GET.get(param)
            for param in self.get_preserved_params()
            if self.request.GET.get(param) is not None}
        return urlencode(params)

    def get_next_url(self):
        return reverse('node-list') + "?" + self.get_preserved_query()

    def get_success_url(self):
        return self.get_next_url()

    def get_form_kwargs(self):
        kwargs = super(NodeListView, self).get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def post(self, request, *args, **kwargs):
        """Handle a POST request."""
        self.populate_modifiers(request)
        return super(NodeListView, self).post(request, *args, **kwargs)

    def form_invalid(self, form):
        """Handle the view response when the form is invalid."""
        self.object_list = self.get_queryset()
        context = self.get_context_data(
            object_list=self.object_list,
            form=form)
        return self.render_to_response(context)

    def form_valid(self, form):
        """Handle the view response when the form is valid."""
        stats = form.save()
        action_name = form.cleaned_data['action']
        if action_name == SetZoneBulkAction.name:
            action_class = SetZoneBulkAction
        else:
            action_class = ACTIONS_DICT[form.cleaned_data['action']]
        message = message_from_form_stats(action_class, *stats)
        messages.info(self.request, message)
        return super(NodeListView, self).form_valid(form)

    def _compose_sort_order(self):
        """Put together a tuple describing the sort order.

        The result can be passed to a node query's `order_by` method.
        Wherever two nodes are equal under the sorter order, creation date
        is used as a tie-breaker: newest node first.
        """
        if self.sort_by not in self.sort_fields:
            order_by = ()
        else:
            custom_order = self.sort_by
            if self.sort_dir == 'desc':
                custom_order = '-%s' % custom_order
            order_by = (custom_order, )
        return order_by + ('-created', )

    def _constrain_nodes(self, nodes_query):
        """Filter the given nodes query by user-specified constraints.

        If the specified constraints are invalid, this will set an error and
        return an empty query set.

        :param nodes_query: A query set of nodes.
        :return: A query set of nodes that returns a subset of `nodes_query`.
        """
        data = _parse_constraints(self.query)
        form = AcquireNodeForm.Strict(data=data)
        # Change the field names of the AcquireNodeForm object to
        # conform to Juju's naming.
        form.rename_fields(JUJU_ACQUIRE_FORM_FIELDS_MAPPING)
        if form.is_valid():
            return form.filter_nodes(nodes_query)
        else:
            self.query_error = ', '.join(
                ["%s: %s" % (field, ', '.join(errors))
                 for field, errors in form.errors.items()])
            return Node.objects.none()

    def get_queryset(self):
        nodes = Node.objects.get_nodes(
            user=self.request.user, perm=NODE_PERMISSION.VIEW)
        nodes = nodes.order_by(*self._compose_sort_order())
        if self.query:
            nodes = self._constrain_nodes(nodes)
        nodes = prefetch_nodes_listing(nodes)
        return configure_macs(nodes)

    def _prepare_sort_links(self):
        """Returns 2 dicts, with sort fields as keys and
        links and CSS classes for the that field.
        """

        # Build relative URLs for the links, just with the params
        fields = self.sort_fields + self.late_sort_fields
        links = {field: '?' for field in fields}
        classes = {field: 'sort-none' for field in fields}

        params = self.request.GET.copy()
        reverse_dir = 'asc' if self.sort_dir == 'desc' else 'desc'

        for field in fields:
            params['sort'] = field
            if field == self.sort_by:
                params['dir'] = reverse_dir
                classes[field] = 'sort-%s' % self.sort_dir
            else:
                params['dir'] = 'asc'

            links[field] += params.urlencode()

        return links, classes

    def late_sort(self, context):
        """Sorts the node_list with sorting arguments that require
        late sorting.
        """
        node_list = context['node_list']
        reverse = (self.sort_dir == 'desc')
        if self.sort_by in self.late_sort_fields:
            node_list = sorted(
                node_list, key=attrgetter(self.sort_by),
                reverse=reverse)
        context['node_list'] = node_list
        return context

    def get_context_data(self, **kwargs):
        context = super(NodeListView, self).get_context_data(**kwargs)
        context = self.late_sort(context)
        form_class = self.get_form_class()
        form = self.get_form(form_class)
        context["preserved_query"] = self.get_preserved_query()
        context["form"] = form
        context["input_query"] = self.query
        context["input_query_error"] = self.query_error
        links, classes = self._prepare_sort_links()
        context["sort_links"] = links
        context["sort_classes"] = classes
        context['power_types'] = generate_js_power_types()
        return context


def enlist_preseed_view(request):
    """View method to display the enlistment preseed."""
    warning_message = (
        "The URL mentioned in the following enlistment preseed will "
        "be different depending on which cluster controller is "
        "responsible for the enlisting node.  The URL shown here is for "
        "nodes handled by the cluster controller located in the region "
        "controller's network."
        )
    context = RequestContext(request, {'warning_message': warning_message})
    try:
        preseed = get_enlist_preseed()
    except NameError as e:
        preseed = "ERROR RENDERING PRESEED\n" + unicode(e)
    return render_to_response(
        'maasserver/enlist_preseed.html',
        {'preseed': mark_safe(preseed)},
        context_instance=context)


class NodeViewMixin:
    """Mixin class used to fetch a node by system_id.

    The logged-in user must have View permission to access this page.
    """

    context_object_name = 'node'

    def get_object(self):
        system_id = self.kwargs.get('system_id', None)
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=self.request.user,
            perm=NODE_PERMISSION.VIEW)
        return node


class NodePreseedView(NodeViewMixin, DetailView):
    """View class to display a node's preseed."""

    template_name = 'maasserver/node_preseed.html'

    def get_context_data(self, **kwargs):
        context = super(NodePreseedView, self).get_context_data(**kwargs)
        node = self.get_object()
        # Display the preseed content exactly as generated by
        # `get_preseed`.  This will be rendered in a <pre> tag.
        try:
            preseed = get_preseed(node)
        except NameError as e:
            preseed = "ERROR RENDERING PRESEED\n" + unicode(e)
        context['preseed'] = mark_safe(preseed)
        context['is_commissioning'] = (
            node.status == NODE_STATUS.COMMISSIONING)
        return context


# Info message displayed on the node page for COMMISSIONING
# or READY nodes.
NODE_BOOT_INFO = mark_safe("""
You can boot this node using an adequately
configured DHCP server.  See
<a href="https://maas.ubuntu.com/docs/nodes.html"
>https://maas.ubuntu.com/docs/nodes.html</a> for instructions.
""")


NO_POWER_SET = mark_safe("""
This node does not have a power type set and MAAS will be unable to
control it. Click 'Edit node' and set one.
""")


THIRD_PARTY_DRIVERS_NOTICE = dedent("""
    Third party drivers may be used when booting or installing nodes.
    These may be proprietary and closed-source.
    """)


THIRD_PARTY_DRIVERS_ADMIN_NOTICE = dedent("""
    The installation of third party drivers can be disabled on the <a
    href="%s#third_party_drivers">settings</a> page.
    """)

UNCONFIGURED_IPS_NOTICE = dedent("""
    Automatic configuration of IPv6 addresses is currently only supported on
    Ubuntu, using the fast installer.  To activate the IPv6 address(es) shown
    here, configure them in the installed operating system.
    """)


def construct_third_party_drivers_notice(user_is_admin):
    """Build and return the notice about third party drivers.

    If `user_is_admin` is True, a link to the settings page will be
    included in the message.

    :param user_is_admin: True if the user is an administrator, False
        otherwise.
    """
    if user_is_admin:
        return mark_safe(
            THIRD_PARTY_DRIVERS_NOTICE +
            THIRD_PARTY_DRIVERS_ADMIN_NOTICE %
            escape(reverse("settings"), quote=True))
    else:
        return mark_safe(THIRD_PARTY_DRIVERS_NOTICE)


class NodeView(NodeViewMixin, UpdateView):
    """View class to display a node's information and buttons for the actions
    which can be performed on this node.
    """

    template_name = 'maasserver/node_view.html'

    def get_form_class(self):
        return get_action_form(self.request.user, self.request)

    # The number of events shown on the node view page.
    number_of_events_shown = 5

    def warn_unconfigured_ip_addresses(self, node):
        """Should the UI warn about unconfigured IPv6 addresses on the node?

        Static IPv6 addresses are configured on the node using Curtin.  But
        this is not yet supported for all operating systems and installers.
        If a node has IPv6 addresses assigned but is not being deployed in a
        way that supports configuring them, the node page should show a warning
        to say that the user will need to configure the node to use those
        addresses.

        :return: Bool: should the UI show this warning?
        """
        supported_os = (node.get_osystem() in OS_WITH_IPv6_SUPPORT)
        if supported_os and node.boot_type == NODE_BOOT.FASTPATH:
            # MAAS knows how to configure IPv6 addresses on an Ubuntu node
            # installed with the fast installer.  No warning needed.
            return False
        # For other installs, we need the warning if and only if the node has
        # static IPv6 addresses.
        static_ips = StaticIPAddress.objects.filter(macaddress__node=node)
        return any(
            IPAddress(static_ip.ip).version == 6
            for static_ip in static_ips)

    def get_context_data(self, **kwargs):
        context = super(NodeView, self).get_context_data(**kwargs)
        node = self.get_object()
        context['can_edit'] = self.request.user.has_perm(
            NODE_PERMISSION.EDIT, node)
        if node.status in (NODE_STATUS.COMMISSIONING, NODE_STATUS.READY):
            messages.info(self.request, NODE_BOOT_INFO)
        if node.power_type == '':
            messages.error(self.request, NO_POWER_SET)
        if self.warn_unconfigured_ip_addresses(node):
            messages.warning(self.request, UNCONFIGURED_IPS_NOTICE)
            context['unconfigured_ips_warning'] = UNCONFIGURED_IPS_NOTICE

        context['error_text'] = (
            node.error if node.status == NODE_STATUS.FAILED_COMMISSIONING
            else None)
        context['status_text'] = (
            node.error if node.status != NODE_STATUS.FAILED_COMMISSIONING
            else None)
        kernel_opts = node.get_effective_kernel_options()
        context['kernel_opts'] = {
            'is_global': kernel_opts[0] is None,
            'is_tag': isinstance(kernel_opts[0], Tag),
            'tag': kernel_opts[0],
            'value': kernel_opts[1]
            }
        # Produce a "clean" composite details document.
        probed_details = merge_details_cleanly(
            get_single_probed_details(node.system_id))
        # We check here if there's something to show instead of after
        # the call to get_single_probed_details() because here the
        # details will be guaranteed well-formed.
        if len(probed_details.xpath('/*/*')) == 0:
            context['probed_details_xml'] = None
            context['probed_details_yaml'] = None
        else:
            context['probed_details_xml'] = etree.tostring(
                probed_details, encoding=unicode, pretty_print=True)
            context['probed_details_yaml'] = XMLToYAML(
                etree.tostring(
                    probed_details, encoding=unicode,
                    pretty_print=True)).convert()

        commissioning_results = NodeResult.objects.filter(
            node=node, result_type=RESULT_TYPE.COMMISSIONING).count()
        context['nodecommissionresults'] = commissioning_results

        installation_results = NodeResult.objects.filter(
            node=node, result_type=RESULT_TYPE.INSTALLING)
        if len(installation_results) > 1:
            for result in installation_results:
                result.name = re.sub('[_.]', ' ', result.name)
            context['nodeinstallresults'] = installation_results
        elif len(installation_results) == 1:
            installation_results[0].name = "install log"
            context['nodeinstallresults'] = installation_results

        context['third_party_drivers_enabled'] = Config.objects.get_config(
            'enable_third_party_drivers')
        context['drivers'] = get_third_party_driver(node)

        event_list = (
            Event.objects.filter(node=self.get_object())
            .exclude(type__level=logging.DEBUG)
            .order_by('-id')[:self.number_of_events_shown])
        context['event_list'] = event_list
        context['event_count'] = Event.objects.filter(
            node=self.get_object()).count()

        return context

    def dispatch(self, *args, **kwargs):
        """Override from Django `View`: Handle MAAS exceptions.

        Node actions may raise exceptions derived from
        :class:`MAASAPIException`.  This type of exception contains an
        http status code that we will forward to the client.
        """
        try:
            return super(NodeView, self).dispatch(*args, **kwargs)
        except MAASAPIException as e:
            return e.make_http_response()

    def get_success_url(self):
        return reverse('node-view', args=[self.get_object().system_id])


class NodeEventListView(NodeViewMixin, PaginatedListView):

    context_object_name = "event_list"

    template_name = "maasserver/node_event_list.html"

    def get_queryset(self):
        return Event.objects.filter(
            node=self.get_object()).order_by('-id')

    def get_context_data(self, **kwargs):
        context = super(NodeEventListView, self).get_context_data(**kwargs)
        node = self.get_object()
        context['node'] = node
        return context


class NodeEdit(UpdateView):

    template_name = 'maasserver/node_edit.html'

    def get_object(self):
        system_id = self.kwargs.get('system_id', None)
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=self.request.user,
            perm=NODE_PERMISSION.EDIT)
        return node

    def get_form_class(self):
        return get_node_edit_form(self.request.user)

    def get_form_kwargs(self):
        # This is here so the request can be passed to the form. The
        # form needs it because it sets error messages for the UI.
        kwargs = super(NodeEdit, self).get_form_kwargs()
        kwargs['request'] = self.request
        kwargs['ui_submission'] = True
        return kwargs

    def get_success_url(self):
        return reverse('node-view', args=[self.get_object().system_id])

    def get_context_data(self, **kwargs):
        context = super(NodeEdit, self).get_context_data(**kwargs)
        context['power_types'] = generate_js_power_types(
            self.get_object().nodegroup)
        return context


class NodeDelete(HelpfulDeleteView):

    template_name = 'maasserver/node_confirm_delete.html'
    context_object_name = 'node_to_delete'
    model = Node

    def get_object(self):
        system_id = self.kwargs.get('system_id', None)
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=self.request.user,
            perm=NODE_PERMISSION.ADMIN)
        if node.status == NODE_STATUS.ALLOCATED:
            raise PermissionDenied()
        return node

    def get_next_url(self):
        return reverse('node-list')

    def name_object(self, obj):
        """See `HelpfulDeleteView`."""
        return "Node %s" % obj.system_id


class MacAdd(CreateView):
    form_class = MACAddressForm
    template_name = 'maasserver/node_add_mac.html'

    def get_node(self):
        system_id = self.kwargs.get('system_id', None)
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=self.request.user,
            perm=NODE_PERMISSION.EDIT)
        return node

    def get_form_kwargs(self):
        kwargs = super(MacAdd, self).get_form_kwargs()
        kwargs['node'] = self.get_node()
        return kwargs

    def form_valid(self, form):
        res = super(MacAdd, self).form_valid(form)
        messages.info(self.request, "MAC address added.")
        return res

    def get_success_url(self):
        node = self.get_node()
        return reverse('node-edit', args=[node.system_id])

    def get_context_data(self, **kwargs):
        context = super(MacAdd, self).get_context_data(**kwargs)
        context.update({'node': self.get_node()})
        return context


class MacDelete(HelpfulDeleteView):

    template_name = 'maasserver/mac_confirm_delete.html'
    context_object_name = 'mac_to_delete'
    model = MACAddress

    def get_node(self):
        system_id = self.kwargs.get('system_id', None)
        node = Node.objects.get_node_or_404(
            system_id=system_id, user=self.request.user,
            perm=NODE_PERMISSION.EDIT)
        return node

    def get_object(self):
        node = self.get_node()
        mac_address = self.kwargs.get('mac_address', None)
        return get_object_or_404(
            MACAddress, node=node, mac_address=mac_address)

    def get_next_url(self):
        node = self.get_node()
        return reverse('node-edit', args=[node.system_id])

    def name_object(self, obj):
        """See `HelpfulDeleteView`."""
        return "MAC address %s" % obj.mac_address
