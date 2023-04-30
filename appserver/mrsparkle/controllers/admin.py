from builtins import range
from builtins import map
from builtins import filter
from builtins import object
from functools import cmp_to_key

import json
import logging
import os, os.path, copy, cgi
import random
import re
import hashlib  # NOQA: Used by some manager XML

from future.moves.urllib import parse as urllib_parse

import cherrypy
import formencode
import lxml.etree as et
import defusedxml.lxml as safe_lxml

import splunk
from splunk import auth
import os
import sys
from splunk.appserver.mrsparkle.controllers.appinstall import AppInstallController
from splunk.appserver.mrsparkle.controllers.licensing import LicensingController
from splunk.appserver.mrsparkle.controllers.summarization import SummarizationController
from splunk.appserver.mrsparkle.controllers.datamodel import DataModelController
from splunk.appserver.mrsparkle.lib.capabilities import Capabilities
from splunk.appserver.mrsparkle.lib import util
from splunk.appserver.mrsparkle.lib import cached
from splunk.appserver.mrsparkle.lib import viewstate
from splunk.appserver.mrsparkle.lib.eai import DynamicUIHelper
from splunk.appserver.mrsparkle.lib.eai import cpUnquoteEntity
from splunk.appserver.mrsparkle.lib.eai import cpQuoteEntity
from splunk.appserver.mrsparkle.lib.i18n import deferred_ugettext
from splunk.appserver.mrsparkle.lib.msg_pool import MsgPoolMgr, UI_MSG_POOL
from splunk.appserver.mrsparkle.controllers import BaseController
from splunk.appserver.mrsparkle.lib.decorators import expose_page
from splunk.appserver.mrsparkle.lib.decorators import set_cache_level
from splunk.appserver.mrsparkle.lib.routes import route
from splunk.util import normalizeBoolean
from splunk.util import parseISO
from splunk.util import safeURLQuote
from splunk.util import unicode
from shutil import copyfileobj
import splunk.entity as en
import splunk.rest as rest

logger = logging.getLogger('splunk.appserver.controllers.admin')

# permissions constants
NOACCESS = 0
READONLY = 1
READWRITE = 2

DEFAULT_CANCEL_URL = '/manager/%(namespace)s'
INSTALL_URL = '/manager/appinstall'
SERVER_ROLES_ENDPOINT = '/services/admin/server-roles?output_mode=json'
SHC_MEMBER = 'shc_member'
SHC_CAPTAIN = 'shc_captain'
DMC_SETTINGS = {
    'url': 'dmc-conf/settings',
    'status': None
}

# View that holds the navigation modules
ADMIN_VIEW_NAME = '_admin'

CONTROL_ACTIONS = {'enable': {
                        'withcontext': deferred_ugettext('Enabled %(item)s in %(context)s.'),
                        'nocontext': deferred_ugettext('Enabled %(item)s.')
                        },
                   'disable': {
                        'withcontext': deferred_ugettext('Disabled %(item)s in %(context)s.'),
                        'nocontext': deferred_ugettext('Disabled %(item)s.')
                    },
                   'remove': {
                        'withcontext': deferred_ugettext('Deleted %(item)s from %(context)s.'),
                        'nocontext': deferred_ugettext('Deleted %(item)s.')
                    },
                   'unembed': {
                        'withcontext': deferred_ugettext('Disabled Embedding %(item)s from %(context)s.'),
                        'nocontext': deferred_ugettext('Disabled Embedding %(item)s.')
                    },
                   'quarantine': {
                        'withcontext': deferred_ugettext('Quarantined %(item)s from %(context)s.'),
                        'nocontext': deferred_ugettext('Quarantined %(item)s.')
                    },
                   'unquarantine': {
                        'withcontext': deferred_ugettext('Unquarantined %(item)s from %(context)s.'),
                        'nocontext': deferred_ugettext('Unquarantined %(item)s.')
                    }}


# define querystring property override prefix
QS_PROP_PREFIX = 'def.'

# define list of prefixes that are to be ignored by override system
QS_PROP_BLACKLIST = ['eai:', '_', 'f_']

# This array should match the array that is used on the client side validation.
AUTHORIZED_FILE_EXTENSIONS = ['jpg', 'jpeg', 'png']

UPLOAD_PATH = util.make_splunkhome_path(['var', 'run', 'splunk', 'apptemp'])

INTERVAL_ELEMENT = """
    <element name="intervalField" type="fieldset">
      <key name="legend">Interval</key>
      <view name="list"/>
      <view name="edit"/>
      <view name="create"/>
      <elements>
        <element name="interval" type="textfield" label="Interval">
          <key name="exampleText">Number of seconds to wait before running the command again, or a valid cron schedule.  (leave empty to run this script once)</key>
          <view name="edit"/>
          <view name="create"/>
        </element>
      </elements>
    </element>
    """

class SplunkbaseException(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)

NODEFAULT = object()
class XMLParseError(Exception): pass

class FlaggedElement(object):
    def __init__(self, value):
        self.value = value

class DisableElement(FlaggedElement):
    """Allow for processValue* statements to force disable an element on a manager page"""
    pass

class EnableElement(FlaggedElement):
    """Allow for processValue* statements to force enable an element on a manager page"""
    pass

def _unpackOnChange(onChange):
    onChange_dict = {}
    for k in onChange:
        if k.tag == 'group_set':
            onChange_dict['_groupset'] = [group.attrib['name'] for group in k]
        elif k.tag == 'value_map':
            onChange_dict['value_map'] = dict([(key.attrib['name'], key.text if key.text else '') for key in k])
        elif k.tag == 'chained_action':
            onChange_dict['_chained_action'] = _unpackOnChange(k)
        else:
            onChange_dict[k.attrib['name']] = k.text

    return onChange_dict


def _unpackOpt(opt):
    type = opt.attrib.get('type')
    if type:
        if type == 'dict':
            return_dict = {}
            for o in opt:
                return_dict[o.attrib['name']] = _unpackOpt(o)
            return return_dict
        elif type  == 'list':
            return_list = []
            for item in opt:
                return_list.append(_unpackOpt(item))
            return return_list

    return opt.text

def _unpackElement(element):
    element_dict = {}
    if 'name' in element.attrib:
        element_dict['elementName'] = element.attrib['name']

    for attr in ('type', 'label', 'class'):
        if not attr in element.attrib:
            continue

        element_dict[attr] = element.attrib[attr]

    onChanges = element.findall('onChange')
    if len(onChanges) == 1:
        element_dict['onChange'] = _unpackOnChange(onChanges[0])
    elif len(onChanges) > 1:
        element_dict['onChange'] = [_unpackOnChange(onChange) for onChange in onChanges]


    for o_type in ('options', 'extraOptions'):
        o_node = element.find(o_type)

        if not o_node:
            continue

        element_dict[o_type] = [{'value': opt.attrib['value'], 'label': _(opt.attrib['label'])} for opt in o_node]

    views = element.findall('view')
    if views:
        element_dict['showInView'] = [view.attrib['name'] for view in views]

    opts = element.findall('key')
    for opt in opts:
        element_dict[opt.attrib['name']] = _unpackOpt(opt)

    elements = element.find('elements')
    if elements:
        element_dict['elements'] = [_unpackElement(e) for e in elements]

    # type=repeatable uses this
    subelement = element.find('element')
    if subelement:
        element_dict['element'] = _unpackElement(subelement)


    return element_dict

def _unpack(root_str, search=None):

    if not root_str: return

    parser = et.XMLParser(remove_blank_text=True, remove_comments=True)
    root = safe_lxml.fromstring(root_str, parser=parser)

    if root.tag != 'endpoint':
        return (None, None)

    if search and root.attrib['name'] != search:
        return (None, None)

    contents = {}

    contents['redirect'] = normalizeBoolean(root.attrib['redirect']) if 'redirect' in root.attrib else True
    if 'hideEnabledColumn' in root.attrib:
        # logger.debug("should hide enabled column.")
        contents['hideEnabledColumn'] = normalizeBoolean(root.attrib['hideEnabledColumn'])
    if 'hidePermissionsColumn' in root.attrib:
        # logger.debug("should hide permissions column.")
        contents['hidePermissionsColumn'] = normalizeBoolean(root.attrib['hidePermissionsColumn'])
    if 'hideActionsColumn' in root.attrib:
        # logger.debug("should hide actions column.")
        contents['hideActionsColumn'] = normalizeBoolean(root.attrib['hideActionsColumn'])
    if 'displayNameField' in root.attrib:
        contents['displayNameField'] = root.attrib['displayNameField']

    contents['header'] = root.findtext('header')
    contents['introText'] = root.findtext('introText')

    breadcrumb = root.find('breadcrumb')
    if breadcrumb:
        breadcrumb_parent_node = breadcrumb.find('parent')
        if breadcrumb_parent_node is not None:
            contents['breadcrumb_parent'] = breadcrumb_parent_node.text
            contents['breadcrumb_hide_current'] = normalizeBoolean(breadcrumb_parent_node.attrib['hidecurrent']) if 'hidecurrent' in breadcrumb_parent_node.attrib else False
        breadcrumb_noentity = breadcrumb.findtext('noentity')
        contents['breadcrumb_noentity'] = normalizeBoolean(breadcrumb_noentity) if breadcrumb_noentity else False
        contents['breadcrumb_name'] = breadcrumb.findtext('name')
        contents['breadcrumb_entityname'] = breadcrumb.findtext('entityname')

    if 'showAppContext' in root.attrib:
        contents['showAppContext'] = root.attrib['showAppContext']

    if 'applyListFilter' in root.attrib:
        contents['applyListFilter'] = root.attrib['applyListFilter']

    menu = root.find('menu')
    if menu:
        menu_dict = {}
        if 'name' in menu.attrib:
            menu_dict['name'] = menu.attrib['name']
        for menu_opt in ('label', 'url', 'description', 'id'):
            menu_opt_node = menu.find(menu_opt)
            if menu_opt_node != None:
                menu_dict[menu_opt] = menu_opt_node.text

        order_node = menu.find('order')
        menu_dict['order'] = 0
        if order_node != None:
            try:
                menu_dict['order'] = int(order_node.text)
            except:
                pass

        link_nodes = menu.findall('link')
        for link in link_nodes:
            link_dict = {}
            for opt in link.findall('key'):
                link_dict[opt.attrib['name']] = _unpackOpt(opt)
            menu_dict.setdefault('links', []).append(link_dict)

        contents['menu'] = menu_dict


    elements = root.find('elements')

    if elements:
        contents['elements'] = [_unpackElement(element) for element in elements]

    if 'template' in root.attrib:
        contents['template'] = root.attrib['template']

    return (root.attrib['name'], contents)


def _extractPathFromURI(uri):
    '''Takes a string that should look like a uri, parses out any scheme and returns just the path and arguments.'''
    import splunk.util
    if uri == None:
        return uri
    elif isinstance(uri, splunk.util.string_type):
        parsed_uri = urllib_parse.urlsplit(uri)
        if parsed_uri:
            parsed_uri = list(parsed_uri)
            parsed_uri[0] = ''
            parsed_uri[1] = ''
            return urllib_parse.urlunsplit(parsed_uri)
    return None

def _cleanseCRLF(uri):
    '''Prevent splunkd request chaining SPL-73544 '''
    import splunk.util
    if uri == None:
        return uri
    elif isinstance(uri, splunk.util.string_type):
        try:
            cleanUri = uri.splitlines()[0]
        except:
            cleanUri = uri
        return cleanUri

def _unpackManagerXmlElementFromStr(xml_str):
    """
    given an XML string that represents a manager xml <element> block, unpacks
    it into a python dict
    """
    parser = et.XMLParser(remove_blank_text=True, remove_comments=True)
    element = safe_lxml.fromstring(xml_str, parser=parser)

    contents = None
    if element is not None:
        contents = _unpackElement(element)
    return contents


def _sourceTypeFieldsForModinput(inputName):
    """
    generates manager xml <element> representing UI fields for
    setting/editing/viewing the sourcetype of a modular input elem
    """
    modinputs_sourcetype_fmt = """
        <element name="sourcetypeFields" type="fieldset">
          <view name="list"/>
          <view name="edit"/>
          <view name="create"/>
          <elements>
            <element name="spl-ctrl_sourcetypeSelect" type="select" label="Set sourcetype">
              <onChange>
                <key name="auto">NONE</key>
                <key name="_action">showonly</key>
                <group_set>
                  <group name="sourcetype"/>
                  <group name="spl-ctrl_from_list"/>
                </group_set>
                <key name="sourcetype">sourcetype</key>
                <key name="spl-ctrl_from_list">spl-ctrl_from_list</key>
              </onChange>
              <options>
                <opt value="auto" label="Automatic"/>
                <opt value="spl-ctrl_from_list" label="From list"/>
                <opt value="sourcetype" label="Manual"/>
              </options>
              <view name="edit"/>
              <view name="create"/>
              <key name="exampleText">Set to automatic and Splunk will classify and assign sourcetype automatically. Unknown sourcetypes will be given a placeholder name.</key>
                      <key name="processValueEdit">[ e for e in ['sourcetype'] if form_defaults.get(e) ]</key>
                      <key name="processValueAdd">[ e for e in ['sourcetype'] if form_defaults.get(e) ]</key>
            </element>
            <element name="sourcetype" type="textfield" label="Source type">
              <view name="list"/>
              <view name="edit"/>
              <view name="create"/>
                      <key name="requiredIfVisible" />
              <key name="exampleText">If this field is left blank, the default value will be used for the source type.</key>
              <key name="processValueList">'%s' if (value==None or value=='') else value</key>
              <key name="submitValueAdd">form_data.get('spl-ctrl_from_list') if form_data.get('spl-ctrl_sourcetypeSelect')=='spl-ctrl_from_list' else value if form_data.get('spl-ctrl_sourcetypeSelect')=='sourcetype' else None</key>
              <key name="submitValueEdit">form_data.get('spl-ctrl_from_list') if form_data.get('spl-ctrl_sourcetypeSelect')=='spl-ctrl_from_list' else value if form_data.get('spl-ctrl_sourcetypeSelect')=='sourcetype' else ''</key>
              <key name="labelList">Source type</key>
            </element>
            <element name="spl-ctrl_from_list" type="select" label="Select source type from list">
              <view name="edit"/>
              <view name="create"/>
                      <key name="exampleText">Splunk classifies all common data types automatically, but if you're looking for something specific, you can find more source types in the <![CDATA[<a href="../../../apps/remote">SplunkApps apps browser</a>]]> or online at <![CDATA[<a href="http://apps.splunk.com/" target="_blank">apps.splunk.com</a>]]>.</key>
              <key name="requiredIfVisible" />
              <key name="dynamicOptions" type="dict">
                <key name="keyName">title</key>
                <key name="keyValue">title</key>
                                <key name="splunkSource">/saved/sourcetypes</key>
                <key name="splunkSourceParams" type="dict">
                  <key name="count">-1</key>
                                  <key name="search">'pulldown_type=true'</key>
                </key>
                <key name="prefixOptions" type="list">
                    <item type="list">
                        <item></item>
                        <item>Choose...</item>
                    </item>
                </key>
              </key>
            </element>
          </elements>
          <key name="legend">Source type</key>
          <key name="helpText">Set sourcetype field for all events from this source.</key>
        </element>
    """
    return modinputs_sourcetype_fmt % inputName


def _is_DMCDisabled():
    """
    Checks if DMC is enabled
    :return bool:
    """
    # return cached value if not none
    if DMC_SETTINGS['status'] is not None:
        return DMC_SETTINGS['status']

    isDMCDisabled = True
    try:
        dmc_settings = en.getEntity(DMC_SETTINGS['url'], 'settings')
        isDMCDisabled = normalizeBoolean(dmc_settings['disabled'])
    except splunk.RESTException as e:
        # SPL-158060: This exception should not be logged as error because we know this
        # is part of normal workflow (DMC url not configured).
        logger.info('Failed to fetch DMC settings to verify status')
    except KeyError as e:  # handle key exception when fetching disabled state.
        logger.error('Failed to fetch DMC disabled status')
        logger.exception(e)
    except ValueError as e:  # handle normalize boolean exception.
        logger.error('Failed to parse disabled status')
        logger.exception(e)

    DMC_SETTINGS['status'] = isDMCDisabled
    return isDMCDisabled


def _is_redirect_safe(url):
    """ See if a string returns a network path location
    """

    # Catch things like https:// http:// file://
    o = urllib_parse.urlparse(url)
    if o.scheme:
        return False

    slash_prefix_cnt = 0

    for c in url:
        if c in ('/', '\\'):
            slash_prefix_cnt += 1
            if slash_prefix_cnt >= 2:
                return False
        elif c in ('\x09', '\x0b'):
            continue
        else:
            return True


class AdminController(BaseController, Capabilities):
    """
    Base handler for Splunk Manager endpoints.  Currently hosted at /manager/*
    """
    appinstall = AppInstallController()

    # The client may hide/show elements based on the accessibility
    # of these endpoints.
    CLIENT_WANTED_ENDPOINTS = ['admin/alert_actions']

    #
    # top level directories
    #

    class SystemShimController(BaseController):
        '''
        Shim controller to host manager resources that do not use the EAI-XML
        manager definitions
        '''
        licensing = LicensingController()
        summarization = SummarizationController()
        datamodel = DataModelController()
    system = SystemShimController()

    def supports_blacklist_validation(self):
        """
        Overridden this method from BaseController!
        """
        return True

    #
    # main handlers
    #

    def generateBreadcrumbs(self, namespace, endpoint_base=None, entity_name=None, uri=None, final=None):
        """
        Generate the list of breadcrumb urls used by the layout template

        This is generally [root] -> [endpoint1] -> [endpoint2] -> [entity]

        Endpoints represent the url for the entity list/summary view.

        There's only two endpoints if endpoint2 supplies a parent endpoint in it's breadcrumb
        definition in uiHelper.

        The uiHelper config can optionally specify a few parameters to affect the breadcrumb definition:
        <breadcrumb>

            <!-- specify that a parent breadcrumb should be inserted and optionally don't display
                 a breadcrumb for the current endpoint -->
            <parent hidecurrent="True">parent_endpoint</parent>

            <!-- override the name used for the endpoint - By default it will use the menu label
                 or the value specified in the header node -->
            <name>List Entities</name>

            <!-- override the name used for entities contained by this endpoint.
                 By default the entity's own name is used, setting this will make them all the same -->
            <entityname>System Setings</entityname>

            <!-- don't display a breadcrumb at all for the entity view -->
            <noentity>True</noentity>

        </breadcrumb>
        """

        chain = []
        # the final url will be generated via make_url() by the template
        base_url = '/manager/%s/' % namespace

        if final:
            # permissions editor is one level deeper than the entity itself
            chain.append( (_(final), None) )
        if endpoint_base:
            # not the manager index page

            if entity_name:
                # viewing an individual entity
                if entity_name == en.NEW_EAI_ENTITY_NAME:
                    entity_name = _('Add new')
                qs = {'action': 'edit'}
                if uri:
                    qs['uri'] = uri
                bc_path = self.make_url([base_url[:-1], endpoint_base, cpQuoteEntity(entity_name)], _qs=qs, translate=False, relative=True)
                chain.append( (entity_name, bc_path) )

            uiHelpers = self.fetchUIHelpers(namespace=namespace)
            while endpoint_base:
                entry = uiHelpers.get(endpoint_base)
                if not entry:
                    break
                if entry.get('breadcrumb_noentity') and chain:
                    # Remove the entity name from the breadcrumb
                    chain.pop()

                elif entry.get('breadcrumb_entityname') and entity_name:
                    # Override the entity name supplied by the endpoint
                    name = _(entry['breadcrumb_entityname']) # this tends to be a name that needs translating
                    if name[0] == '$': # variable entity name; typicaly $namespace
                        name = locals().get(name[1:], entity_name)
                    chain[0] = (name, None)

                if not entry.get('breadcrumb_hide_current'):
                    if 'menu' in entry:
                        # use the menu label by default
                        title = entry['menu']['label']
                        url = entry['menu']['url'] % {'namespace':namespace,'currentUser': cpQuoteEntity(auth.getCurrentUser()['name'])}

                    elif entry.get('breadcrumb_name'):
                        # else see if there's a breadcrumb override
                        title = entry['breadcrumb_name']
                        url = base_url+endpoint_base

                    elif 'header' in entry:
                        # else use the name defined by the header tag
                        title = entry['header']
                        url = base_url+endpoint_base

                    chain.append( (_(title), url) )
                # see if there's a link to a parent endpoint
                endpoint_base = entry.get('breadcrumb_parent')

        chain.append( (_('Settings'), base_url) )

        if chain[0][1]:
            # Last entry should never be linked.
            chain[0] = (chain[0][0], None)

        # reverse into the correct order
        chain.reverse()
        return chain


    def generateUIHelperFromEntity(self, entity, namespace='search'):
        uiHelper = {}
        uiHelper["header"] = "%s setup" % namespace
        uiHelper["elements"] = []
        hasInputElements = False
        setupXml = entity.get("eai:setup", None)
        # logger.debug(setupXml)
        import xml.etree.cElementTree as et
        from defusedxml.cElementTree import fromstring as safe_fromstring
        root = safe_fromstring(setupXml)

        block_nodes = root.findall('block')

        i = 0
        for block in block_nodes:
            # Refer to JIRA STEWIE-531 for the all details regarding this change !!
            # code below allows apps to have setup.xml to define a custom setup UI, example of setup.xml below:
            # <setup><block task="mycustomsetupui" type="redirect"/></setup>
            # Pickup the custom partial URL from task attribute of block and redirect to custom setup ui
            # <locale>/app/<app-name>/<value of task attribute>?redirect_to_custom_setup=1
            # Look in view.py for consumption of redirect_to_custom_setup query param, which is used to bypass
            # the setup dialog !!
            if 'task' in block.attrib and 'type' in block.attrib and block.attrib['type'] == 'redirect':
                redirect_to = '/app/%s/%s' % (namespace, block.attrib['task'])
                logger.info("Redirecting to custom setup for app=%s url=%s" % (namespace, redirect_to))
                self.redirect_to_url(redirect_to, _qs={"redirect_to_custom_setup":"1"})
                return

            fieldsetElement = {
                "elementName"   : "blockFieldset-%s" % i,
                "showInView"    : [ "edit" ],
                "type"          : "fieldset",
                "legend"        : block.get("title", "I am Legend"),
                "elements"      : []
            }

            j = 0
            for node in block:

                # go through everything in the block and add to this fieldset
                logger.debug(node.tag)
                if node.tag == "text":
                    # for <text> nodes, put them into a type: helpstring
                    element = {
                        "elementName"   : "textNode-%s-%s" % (i, j),
                        "type"          : "helpstring",
                        "helpText"      : node.text,
                        "showInView"    : [ "edit" ]
                    }
                    fieldsetElement["elements"].append(element)

                elif node.tag == "input":
                    # for <input> nodes, put them into the appropriate type per the input
                    type = node.find("type").text
                    if type == "bool":
                        elementType = "checkbox"
                    elif type == 'password':
                        elementType = "password"
                    else:
                        elementType = "textfield"
                    elementLabel = node.find("label").text
                    element = {
                        "elementName"   : node.get("id"),
                        "type"          : elementType,
                        "label"         : elementLabel,
                        "showInView"    : [ "edit" ]
                    }
                    fieldsetElement["elements"].append(element)
                    hasInputElements = True

                j = j + 1

            uiHelper["elements"].append(fieldsetElement)

            i = i + 1

        if not hasInputElements:
            uiHelper["_noInputElements"] = True
        return uiHelper


    def fetchUIHelpers(self, namespace='search', search=None, refresh=0):
        """
        Helper routine to get the ui helpers from EAI.
        """

        try:
            if refresh:
                util.auto_refresh_ui_assets('data/ui/manager')
                helper_en = en.getEntities('data/ui/manager', count=-1, namespace=namespace)
            else:
                helper_en = cached.getEntities('data/ui/manager', count=-1, namespace=namespace)

        except splunk.ResourceNotFound:
            if util.isLite():
                raise cherrypy.HTTPError(404)
            else:
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', 'Unable to locate the configuration for this URL.')
                return self.redirect_to_url("/manager/%s" % splunk.getDefault('namespace'), _qs={
                    "msgid" : msgid
                } )


        uiHelper = {}

        for e in helper_en:
            if len(helper_en[e].get('eai:data')) == 0:
                continue
            (key, val) = _unpack(helper_en[e].get('eai:data'), search)
            if search:
                if key == search:
                    return val
            elif key:
                uiHelper[key] = val

        uiHelper = self.addModularInputsUIHelpers(uiHelper, namespace, search)

        return uiHelper

    def addModularInputsUIHelpers(self, uiHelper, namespace='search', search=None):
        try:
            modularInputs = en.getEntities('data/modular-inputs', count=-1, namespace=namespace)
        except splunk.AuthorizationFailed:
            modularInputs = {}
        inputName = None
        if search:
            segments = search.split('/')
            inputName = segments[-1]
        for e in modularInputs:
            if inputName:
                if e == inputName:
                    uiHelper = self.constructModularInputUIHelper(e, modularInputs.get(e))
                    return uiHelper
            else:
                key = 'data/inputs/' + str(e)
                value = self.constructModularInputUIHelper(e, modularInputs.get(e))
                if key in uiHelper:
                    logger.debug("modular input has a user defined xml.  Skipping auto-generation of UI")
                    continue
                uiHelper[key] = self.constructModularInputUIHelper(e, modularInputs.get(e))


        return uiHelper

    def constructModularInputUIHelper(self, inputName, endpoint_response):
        """
        The modular inputs do not have any hard-coded manager xml, because they are custom defined by the user.  This function iterates through the endpoint for a particular modular
        input, extracts the parameters and appends them to a manually created uiHelper object.
        """

        params = endpoint_response.properties['endpoint']['args']
        useSingleInstance = False
        if endpoint_response.properties['use_single_instance'] in ("1", "true"):
            useSingleInstance = True

        # order the elements by sorting on the 'order' field
        attr = []
        for (paramName, attributes) in params.items():
            attr.append((paramName, attributes))
        ordered_attr = sorted(attr, key = lambda k : int(k[1]['order']))

        elements = []

        headerElement = {'elementName':'spl-ctrl_header', 'type':'header', 'title': 'This is a title', 'showInView':['create', 'edit']}
        if endpoint_response.properties['description']:
            headerElement['body'] = endpoint_response.properties['description']
        elements.append(headerElement)

        for (paramName, attributes) in ordered_attr:
            element = {'elementName': paramName,
                       'type': 'textfield',
                       'label': attributes['title'],
                       'showInView': ['create', 'list'] if paramName == 'name' else ['create', 'list', 'edit'],
                      }
            if attributes['description']:
                element['helpText'] = attributes['description']

            # Use a checkbox for boolean value
            if attributes['data_type'] == 'boolean':
                element['type'] = 'checkbox'

            elements.append(element)

        # Advanced options
        moreSettingsCheckBox = {'elementName': 'spl-ctrl_EnableAdvanced',
            'showInView': ['edit', 'create'],
            'label': 'More settings',
            'onChange': {'1': 'ALL', '0': 'NONE', '_action': 'showonly', '_groupset': ['advanced']},
            'type': 'checkbox',
            'class': 'spl-mgr-advanced-switch'}
        elements.append(moreSettingsCheckBox)

        advanced = {'elementName': 'advanced',
            'showInView': ['edit', 'create', 'list'],
            'type': 'fieldset',
            'class': 'spl-mgr-advanced-options',
            'elements': []}

        indexFields = {'elementName': 'indexField', 'elements': [{'elementName': 'index', 'showInView': ['edit', 'create'], 'label': 'Set the destination index', 'exampleText': "Create an index in Manager > Indexes and it will appear in this list.  Consider creating a test index when you're putting a new type of data into Splunk.", 'type': 'select', 'dynamicOptions': {'keyName': 'title', 'keyValue': 'title', 'splunkSourceParams': {'count': '-1', 'search': "'isInternal=false'"}, 'splunkSource': '/data/indexes'}}], 'showInView': ['edit', 'create'], 'helpText': "When Splunk has consumed your data, it goes into an index. By default, Splunk puts it in the 'main' index, but you can specify a different one.", 'type': 'fieldset', 'legend': 'Index'}

        indexFields = {
            'elementName': 'indexField',
            'elements': [
                {
                    'elementName': 'index',
                    'showInView': ['list', 'edit', 'create'],
                    'type': 'select',
                    'dynamicOptions': {'keyName': 'title', 'keyValue': 'title', 'splunkSourceParams': {'count': '-1', 'search': "'isInternal=false'"}, 'splunkSource': '/data/indexes'},
                    'label': 'Index'
                }
            ],
            'showInView': ['list', 'edit', 'create'],
            'helpText': 'Set the destination index for this source.',
            'type': 'fieldset',
            'legend': 'Index'
        }

        hostfields = {
            'elementName': 'hostFields',
            'showInView': ['edit', 'create'],
            'type': 'fieldset',
            'legend': 'Host',
            'elements': [
                {
                    'helpText': 'Set the host with this value.',
                    'elementName': 'host',
                    'showInView': ['edit', 'create'],
                    'type': 'textfield',
                    'label': 'Host'
                }
            ]
        }

        # add the interval field, but only for "One script instance per input stanza" mode
        # type of inputs
        if not useSingleInstance:
            intervalField = _unpackManagerXmlElementFromStr(INTERVAL_ELEMENT)
            advanced['elements'].append(intervalField)

        # generate the sourcetype-related fields
        sourcetypeFields = _unpackManagerXmlElementFromStr(_sourceTypeFieldsForModinput(inputName))
        advanced['elements'].append(sourcetypeFields)

        advanced['elements'].append(hostfields)
        advanced['elements'].append(indexFields)

        elements.append(advanced)

        uiHelper = {'redirect': True,
            'breadcrumb_hide_current': False,
            'header': endpoint_response.properties['title'],
            'breadcrumb_entityname': None,
            'breadcrumb_name': endpoint_response.properties['title'],
            'breadcrumb_parent': 'datainputstats',
            'breadcrumb_noentity': False,
            'introText': None,
            'elements': elements,
          }

        return uiHelper


    def getElementPermissions(self, endpoint_path, entity_name, element_name, entity):
        """
        Return the current user's permissions for accessing a given form element
        XXX This obviously needs some work ;-)
        """
        # SPL-37054
        if (endpoint_path.startswith('saved/searches') and
           element_name == 'alertgroup' and
           ('alert.severity' not in entity)):
           return NOACCESS
        return READWRITE


    def getCancelURL(self, namespace='search', **kwargs):
        """
        Get the cancel URI for a given admin section page.
        Attempts to elegantly retrieve a best fit redirect url for the cancel button.
        """
        # redirect url
        url = None
        # the referer
        referer = cherrypy.request.headers.get('Referer', None)
        # the session stored manager_cancel_redirect or default if None
        session_url = cherrypy.session.get('manager_cancel_url', DEFAULT_CANCEL_URL % {'namespace': namespace})
        # no referer use session or default
        if referer is None:
            url = session_url
            # SPL-131344, SPL-136266: starting from Kimono, Splunk is blocking referers.
            # Use URL paramater manager_cancel_url instead of referer.
            if 'manager_cancel_url' in kwargs:
                url = kwargs['manager_cancel_url']
        # has a referer
        else:
            # this a referal from itself use session or default
            if referer.find(safeURLQuote(cherrypy.request.path_info)) > -1:
                url = session_url
            # this is a referal from the same machine update session cancel url with referer
            elif referer.find(cherrypy.request.base) > -1:
                trim_referer = referer.replace(cherrypy.request.base, '')
                # strip /prefix/en-US from the url so it's not added back by redirect later
                url = cherrypy.session['manager_cancel_url'] = self.strip_url(trim_referer)
            # in sso mode, the referer may not be from same ip address
            else:
                parsed_referer = urllib_parse.urlsplit(referer)
                # novice way to check if it is a invalid url / path
                if len(parsed_referer.hostname) == 0 or len(parsed_referer.path) == 0:
                    url = session_url
                else:
                    # SPL-55355 pylint error:  Instance of 'SplitResult' has no 'query' member (but some types could not be inferred)
                    referer_query = getattr(parsed_referer, 'query', '')
                    url = cherrypy.session['manager_cancel_url'] = self.strip_url(parsed_referer.path + '?' + referer_query)
        return url


    def saveUploadFileEntity(self, args, namespace):
        """
        Stream a file upload to Splunk for the data/inputs/monitor endpoint
        """
        fs = args.get('spl-ctrl_remotefile')
        if not (isinstance(fs, cherrypy._cpreqbody.Part) and fs.file):
            return False, _('No file was uploaded')

        host = args.get('host')
        host_regex = args.get('host_regex')
        host_segment = args.get('host_segment')
        sourcetype = args.get('sourcetype')
        index = args.get('index', 'main')

        try:
            line = fs.file.readline()
            if not line:
                return False, _("File is empty or doesn't exist")
        except Exception as e:
            return False, str(e)


        try:
            try:
                fh = None
                fh = splunk.input.open(hostname=host, host_regex=host_regex, host_segment=host_segment, sourcetype=sourcetype, index=index, source=os.path.basename(fs.filename) if fs.filename else 'webform')
                fh.write(line)
                for line in fs.file:
                    fh.write(line)
            except Exception as e:
                raise
            finally:
                if fh:
                    fh.close() #Bad things can happen here
        except Exception as e: # Would be good to be smarter here
            return False, str(e)
        else:
            return True, ''



    def saveUploadAppAsset(self, args, namespace):
        """
        Stream a file upload to Splunk for the data/inputs/monitor endpoint
        """
        fs = args.get('spl-ctrl_bg_app')
        if isinstance(fs, cherrypy._cpreqbody.Part) and fs.file and fs.filename != '':
            self.writeAsset(args, fs, 'bg_app.png', namespace)

        fs = args.get('spl-ctrl_image')
        if isinstance(fs, cherrypy._cpreqbody.Part) and fs.file and fs.filename != '':
            self.writeAsset(args, fs, '',  namespace)

        fs = args.get('spl-ctrl_application_css')
        if isinstance(fs, cherrypy._cpreqbody.Part) and fs.file and fs.filename != '':
            self.writeAsset(args, fs, 'application.css', namespace)

        fs = args.get('spl-ctrl_upload1')
        if isinstance(fs, cherrypy._cpreqbody.Part) and fs.file and fs.filename != '':
            self.writeAsset(args, fs, '', namespace)

        fs = args.get('spl-ctrl_upload2')
        if isinstance(fs, cherrypy._cpreqbody.Part) and fs.file and fs.filename != '':
            self.writeAsset(args, fs, '', namespace)


    def writeAsset(self, args, fs, filename, namespace):
        try:
            logger.debug('about to copy to file %s ' % fs.filename)
            tempPath = UPLOAD_PATH #util.make_splunkhome_path(['var', 'run', 'splunk', 'apptemp'])
            if not os.path.exists(tempPath):
                os.makedirs(tempPath)

            if not filename or filename =='':
                filename = fs.filename

            # make sure we have no funny chars
            # in practice protecting for windows filenames is byeond my interest at this time.
            # mitch showed me this and i got scared http://msdn.microsoft.com/en-us/library/aa365247.aspx
            # some python code to validate window filenames would be nice.
            if filename.find('/')>-1 or filename.find('\\')>-1 or filename.startswith('.'):
                logger.warn('Not going to upload files with funny chars %s' % filename)
                # should probably throw something other than return
                return
            newPath = os.path.join(tempPath, filename)
            newFile = open ( newPath, 'wb')
            logger.debug('about to copy across %s to %s ' % (filename, newPath ))
            while True:
                buf = fs.file.read(1024)
                if buf:
                    newFile.write(buf)
                else:
                    break
            #for line in fs.file:
            #    newFile.write(line)
            newFile.close()

        except Exception as e: # Would be good to be smarter here
            logger.warn('Could not get uploaded file %s' % str(e))
            return False, str(e)

        return newPath

    def saveUploadLookupTableFile(self, args, namespace):
        """
        Write a lookup file into $SPLUNK_HOME/var/run/splunk/lookup_tmp and
        forward that file on to splunkd.
        """
        filename = args.get('name')
        if not filename or (len(filename) == 0):
            return False, _('No filename was given')
        """
        SPL-102738: While uploading files, we read/write them line by line.
        However, for binary files(.kmz/.gz), this way will manually insert
        line breakers, which can mess-up the uploaded file. For these cases,
        we need to use block read/write and open the file in "wb" mode.
        """
        isBinary = False
        if filename.endswith(".kmz") or filename.endswith(".gz"):
            isBinary = True
        # Unique-ify filename.
        filename += str(random.random())[1:]

        fs = args.get('spl-ctrl_lookupfile')

        if not (isinstance(fs, cherrypy._cpreqbody.Part) and fs.file):
            return False, _('No file was uploaded')

        try:
            line = fs.file.readline()
            if not line:
                return False, _("File is empty or doesn't exist")
            # Upload to a temporary location.
            lookupDir = util.make_splunkhome_path(['var', 'run', 'splunk', 'lookup_tmp'])
            if not os.path.exists(lookupDir):
                os.makedirs(lookupDir, 0o755)
            dst = os.path.join(lookupDir, filename)
            # Make sure we're not being fooled into writing outside of the
            # staging area for lookup table files.
            dstDirNorm = os.path.dirname(os.path.normpath(dst))
            lookupDirNorm = os.path.normpath(lookupDir)
            if dstDirNorm != lookupDirNorm:
                return False, _('Name cannot contain path separators or relative path notation like ".."')
            # Check if our temporary file collides with another temporary file.
            if os.path.exists(dst):
                return False, _('Temporary lookup table file already exists: %s' % dst)
            if isBinary:
                # Read binary file block by block
                with open(dst, "wb") as out:
                    fs.file.seek(0)
                    while True:
                        block = fs.file.read(1024)
                        if not block:
                            break
                        out.write(block)
            else:
                with open(dst, "wb") as out:
                    # Write out first line.
                    out.write(line)
                    # Write out remainder of file.
                    for line in fs.file:
                        out.write(line)
            # Tell splunkd where to pick up new lookup table file.
            args['eai:data'] = dst
        except Exception as e:
            logger.error('Could not save uploaded lookup file %s' % e.args[0])
            return False, str(e)

        return True, ''

    def saveEntity(self, endpoint_path, entity_name, args, namespace, entity_owner, entity_uri):
        logger.debug('Saving Entity to namespace=%s owner=%s endpoint_path=%s entity_name=%s args=%s' % (namespace, entity_owner, endpoint_path, entity_name, str(args)[:1000]))

        err = ""
        bolSaved = False

        if endpoint_path=='data/inputs/monitor' and entity_name==en.NEW_EAI_ENTITY_NAME and args.get('spl-ctrl_switcher')=='uploadfile':
            return self.saveUploadFileEntity(args, namespace)

        if endpoint_path=='apps/local':
            self.saveUploadAppAsset(args, namespace)
        if endpoint_path=='data/lookup-table-files':
            uploaded, upload_err = self.saveUploadLookupTableFile(args, namespace)
            if not uploaded:
                return uploaded, upload_err

        if endpoint_path.startswith('deployment/server/setup/data/inputs/remote_monitor'):
            appName = {"app_name": args['app_name']}
            entity_uri += '?' + urllib_parse.urlencode(appName)

        #construct the entity to update
        updatedEnt = en.getEntity(endpoint_path, entity_name, namespace=namespace, uri=_extractPathFromURI(entity_uri))
        eaiAttributes = updatedEnt.get("eai:attributes")

        """
        for item in updatedEnt:
            logger.debug("%s : %s" % (item, updatedEnt[item]) )
        """

        updatedEnt.properties = {}
        # set the entity owner to the current user so it will save down to their namespace.
        # currently the editor saves into their namespace, they now own it.  this may need
        # to be expanded into a function when an admin will have the ability to save into
        # someone else's namespace.
        if entity_owner:
            updatedEnt.owner = entity_owner
        else:
            pwnr = auth.getCurrentUser()['name']
            updatedEnt.owner = pwnr

        for arg in args:
            # logger.debug("Value from args is %s=%s" % (arg, args[arg]) )

            # if ( (not arg.startswith('spl-ctrl_')) and (arg!='splunk_session_id') and (arg!=None) and (arg!='confirm_password') and (args[arg] != "") ):
            if ( (not arg.startswith('spl-ctrl_')) and (arg!='splunk_form_key') and (arg!=None) and (arg!='confirm_password') ):
                # don't send empty password string
                if not (arg=='password' and args[arg]==''):
                    updatedEnt[arg] = args[arg]
                # updatedEnt[arg] = args[arg]
                # logger.debug("updatedEnt[%s]=%s" % (arg, updatedEnt[arg]) )

        # strip entries that are not either required, optional or match a wildcard
        if eaiAttributes:
            optionalOrRequiredFields = eaiAttributes['optionalFields'] + eaiAttributes['requiredFields'] + ['name']
            wildcardFields = eaiAttributes.get('wildcardFields', [])
            for key in list(updatedEnt.keys()): # updatedEnt is not instance of a dict. We wrapped the call to keys() in a list() for py3's dictionary changed size during iteration.
                if key not in optionalOrRequiredFields:
                    for wildcard in wildcardFields:
                        if re.match(wildcard, key):
                            break
                    else:
                        logger.debug("Removing non optional/required key %s from entity to be saved" % key)
                        del updatedEnt.properties[key]
        else:
            logger.error("Failed to find eai:attributes for endpoint=%s entity=%s" % (endpoint_path, entity_name))

        try:
            # bolSaved returns True if successful, False if not.
            # logger.debug("updatedEnt %s." % str(updatedEnt))
            msgObj = {'messages':None}
            bolSaved = en.setEntity(updatedEnt, uri=_extractPathFromURI(entity_uri), msgObj=msgObj)

            # we need to refresh the user info for some object types
            if endpoint_path == 'authentication/changepassword' or endpoint_path == 'authentication/users':
                user = splunk.entity.getEntity('authentication/users', cherrypy.session['user']['name'])
                fullName = cherrypy.session['user']['name']
                if user and 'realname' in user and user['realname']:
                    fullName = user['realname']
                cherrypy.session.escalate_lock()
                user_session = cherrypy.session['user']
                user_session['fullName'] = fullName
                cherrypy.session['user'] = user_session

            logger.debug("msgObj was updated in setEntity: %s" % msgObj)
            if (msgObj['messages'] is not None):
                err = msgObj

        except splunk.RESTException as e:
            err = e.get_message_text()
            logger.debug("Fail in saveEntity. msg text: %s." % err)

        except Exception as e:
            err = e
            logger.exception(e)

        return bolSaved, err

    def flattenElements(self, elements, result):
        """Build a elementname=>element mapping"""
        if elements:
            for element in elements:
                if 'elements' in element:
                    if 'elementName' in element:
                        result[element['elementName']] = { 'type': 'fieldset' }
                    self.flattenElements(element['elements'], result)
                else:
                    result[element['elementName']] = element
        return result

    def loadUniqueWidgets(self):
        """
        Find an example of each kind of widget used in uiHelpers, along
        with the configuration for each
        Returns a dictionary of widget configs keyed on widget type name
        """
        uiHelpers = self.fetchUIHelpers()
        widgets = {}
        for elements in [endpoint['elements'] for endpoint in uiHelpers.values() if 'elements' in endpoint]:
            for element in elements:
                widgets.setdefault(element['type'], element)
        return widgets

    def mergeUiHelperWithData(self, uiHelper, entity_name):
        pass

    def getSingleEntity(self, endpoint_path, uri=None, namespace=splunk.getDefault('namespace'), viewFilter="edit"):
        # return values
        form_defaults = None
        uiHelper = None
        entity_name = None
        fetchStaticHelpers = True
        statusCode = 0
        msgid = None

        # note this does not send an owner of None, it just uses the default system user
        # this gets overridden for app setup to use the owner "nobody"
        pwnr = None
        logger.debug("endpoint_path:: %s." % endpoint_path)
        segments = endpoint_path.split("/")
        # chop off the last one to use for the entity name, use the rest for the path.
        entity_name = cpUnquoteEntity(segments.pop())
        endpoint_base = "/".join(segments)
        # logger.debug("endpoint_base: %s." % endpoint_base)
        uri = _cleanseCRLF(uri)
        if entity_name == en.NEW_EAI_ENTITY_NAME:
            if namespace == '-':
                namespace = splunk.getDefault("namespace")
            if pwnr == '-':
                pwnr = None

        try:
            entity = en.getEntity(endpoint_base, entity_name, uri=_extractPathFromURI(uri), namespace=namespace)

            # now that we are using the cpUnquoteEntity method to get the entity name from the url we don't need to do this.
            # entity_name = entity.name

        except splunk.ResourceNotFound as e:
            raise cherrypy.HTTPError(404, _('Splunk cannot find "%s/%s". %s')
                % (endpoint_base, entity_name, e))
        except splunk.AuthorizationFailed as e:
            raise
        except splunk.RESTException as e:
            logger.debug("RESTException.")
            logger.debug(e.statusCode)
            statusCode = e.statusCode
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error',
                            '%s' % e.get_message_text() )
            form_defaults = {}
            entity = {}
            uiHelper = self.fetchUIHelpers(search=endpoint_base, namespace=namespace)

            return uiHelper, form_defaults, entity, statusCode, msgid

        form_defaults = entity

        if endpoint_base.startswith("apps/local/") and entity_name == "setup":
            fetchStaticHelpers = False

        if fetchStaticHelpers:
            uiHelper = self.fetchUIHelpers(search=endpoint_base, namespace=namespace)
        else:
            uiHelper = self.generateUIHelperFromEntity(entity, namespace=namespace)

        if len(uiHelper) == 0:
            logger.error('getSingleEntity - unable to load the section form definition for endpoint=%s' % endpoint_base)

        # Merge uihelper and entity data to create dictionary for template use.
        if "elements" in uiHelper:
            # XXX We'll probably want to change this once the roles stuff is figured out
            def filterElements(elements):
                elementsToFilter = []
                elnum = 0

                for element in elements:
                    # logger.debug("elementName:: %s." % str(element.get("elementName", None)) )
                    # logger.debug("showInView:: %s." % str(element.get("showInView", [])) )

                    if 'elementName' in element and (viewFilter in element.get("showInView", [])):

                        if self.getElementPermissions(endpoint_path, entity_name, element['elementName'], entity) == NOACCESS:
                            elementsToFilter.append(element["elementName"])
                        else:

                            eaiAttributes = entity.get("eai:attributes")
                            if 'disableIfNotInEAIAttributes' in element:
                                disableElement = False
                                if ((element.get('type') != 'fieldset') and
                                    ( not element.get('elementName').startswith("__") ) and
                                    (element.get('elementName') not in eaiAttributes['optionalFields'])  and
                                    (element.get('elementName') not in eaiAttributes['requiredFields']) ):

                                    disableElement = True

                                    if 'wildcardFields' in eaiAttributes:

                                        matches = 0
                                        for regex in eaiAttributes['wildcardFields']:
                                            result = re.search(regex, element.get('elementName'))
                                            if result:
                                                matches = matches + 1
                                        ## if there were any regex matches don't disable the field, it is a wildcard field.
                                        if matches > 0:
                                            disableElement = False

                                ## if the disableElement bool was toggled, disable the element
                                if disableElement:
                                    element["disabled"] = 1

                            value = form_defaults.get(element['elementName'])
                            # logger.debug("value from form_defaults: %s." % value)
                            processValue = 'processValueAdd' if (entity_name == en.NEW_EAI_ENTITY_NAME and not uri) else 'processValueEdit'
                            if processValue in element:
                                try:
                                    # for referenced libs to work from processValue*,
                                    # find all locally imported modules and make a dictionary of name:module and
                                    # make scope of local vars + those modules for eval
                                    modulenames = set(sys.modules) & set(globals())
                                    allmodules = dict([(name, sys.modules[name]) for name in modulenames])
                                    scope = {**locals(), **allmodules}
                                    value = eval(element[processValue].strip(), scope)
                                    logger.debug("processed value: %s" % value)
                                except Exception as e:
                                    logger.error('uiHelper %s operator failed for endpoint_path=%s elementName=%s: %s' % (processValue, endpoint_path, element['elementName'], str(e)))
                                form_defaults[element['elementName']] = value
                            element["value"] = value

                        if "disabledInView" in element:
                            if viewFilter in element["disabledInView"]:
                                element["disabled"] = "1"

                        if isinstance(element.get('value'), FlaggedElement):
                            # Allow for ProcessValue* to disable/enable an element by wrapping value into a EnableElement/DisableElement object
                            # extract the true value from the wrapper
                            value, element['value'] = element['value'], element['value'].value
                            if isinstance(value, EnableElement):
                                element['disabled'] = '0'
                            elif isinstance(value, DisableElement):
                                element['disabled'] = '1'

                        if 'elements' in element:
                            # recurse through elements that contain other elements (eg. fieldset)
                            filterElements(element['elements'])
                    else:
                        elementsToFilter.append(elements[elnum]["elementName"])
                    elnum += 1

                def removeFromHelper(elements, elementsToFilter):
                    for i in range(len(elements)-1, -1, -1):
                        if elements[i]["elementName"] in elementsToFilter:
                            # fieldsets must contain the view for ALL subfields.
                            del elements[i]
                        elif "elements" in elements[i]:
                            # rerun for grouped elements
                            removeFromHelper(elements[i]["elements"], elementsToFilter)

                removeFromHelper(uiHelper["elements"], elementsToFilter)

            # filter out elements that shouldn't be displayed
            # logger.debug("entity_name: %s" % entity_name)
            # logger.debug("viewFilter: %s" % viewFilter)

            filterElements(uiHelper['elements'])

        return uiHelper, form_defaults, entity, statusCode, msgid


    def render_admin_template(self, template_name, template_args=None, hide_back=False):
        """
        Render a template including arguments required to display module based navigation
        """
        root = cherrypy.request.app.root
        namespace = template_args.get('namespace', splunk.getDefault('namespace'))
        # set build_nav=False below as manager doesn't display any modules requiring nav/saved searches
        args =  root.app.buildViewTemplate(namespace, ADMIN_VIEW_NAME, render_invisible=True, build_nav=False, include_app_css_assets=False)
        args['accessible_endpoints'] = self.select_accessible_endpoints(self.CLIENT_WANTED_ENDPOINTS)

        if template_args:
            args.update(template_args)

        if hide_back:
            # surpress the "Return to <appname>" link
            args['modules']['appHeader'][0]['mode'] = 'lite_noback'

        if 'endpoint_path' in args and args['endpoint_path'] == 'apps/local':
            args['can_view_remote_apps'] = self.can_access_manager_xml('appsremote')
            args['isDMCDisabled'] = _is_DMCDisabled()

        # server info
        server_info = splunk.rest.payload.scaffold()
        server_info['entry'][0]['content']['isFree'] = cherrypy.config['is_free_license']
        server_info['entry'][0]['content']['isTrial'] = cherrypy.config['is_trial_license']
        server_info['entry'][0]['content']['version'] = cherrypy.config['version_number']
        server_info['entry'][0]['content']['guid'] = cherrypy.config['guid']
        server_info['entry'][0]['content']['build'] = cherrypy.config['build_number']
        server_info['entry'][0]['content']['staticAssetId'] = cherrypy.config['staticAssetId']
        server_info['entry'][0]['content']['product_type'] = cherrypy.config['product_type']
        server_info['entry'][0]['content']['serverName'] = cherrypy.config['serverName']
        server_info['entry'][0]['content']['instance_type'] = cherrypy.config['instance_type']
        server_info['entry'][0]['content']['addOns'] = cherrypy.config['addOns']
        server_info['entry'][0]['content']['activeLicenseSubgroup'] = cherrypy.config['activeLicenseSubgroup']

        # add splunkd partial payloads
        splunkd = {}
        splunkd['/services/server/info'] = server_info
        args['splunkd'] = splunkd

        return self.render_template(template_name, args)


    @expose_page(must_login=True, methods=['GET'])
    def index(self, **kwargs):
        return self.redirect_to_url('/manager/%s/' % splunk.getDefault('namespace'))

    @route('/:namespace')
    @expose_page(must_login=True, methods=['GET'])
    @set_cache_level('never')
    def nsindex(self, namespace, **kwargs):
        """
        displays list of available entities which can be viewed / managed through this interface
        or some other type of static / dynamic admin home page
        """

        if util.isLite():
            raise cherrypy.HTTPError(404)

        if namespace == "-":
            uiHelperNamespace = splunk.getDefault("namespace")
        else:
            uiHelperNamespace = namespace

        logger.debug("\n" * 5)
        logger.debug(uiHelperNamespace)
        logger.debug("\n" * 5)

        uiHelpers = self.fetchUIHelpers(namespace=uiHelperNamespace, refresh=1)
        blueLinks = {'system_configurations': {'description': _("Manage configuration of this Splunk server."),
                                               'menuItems': []},
                     'app_configurations': {'description': _("View or edit your app configs."),
                                            'menuItems': []},
                     'knowledge_configurations': {'description': _("View or edit your app configs."),
                                            'menuItems': []},
                     'data_configurations': {'description': _("View or edit your app configs."),
                                            'menuItems': []},
                     'deployment_configurations': {'description': _("View or edit your app configs."),
                                            'menuItems': []},
                     'auth_configurations': {'description': _("View or edit your app configs."),
                                            'menuItems': []},
                                            }

        for endpoint_name in uiHelpers:
            endpoint = uiHelpers[endpoint_name]
            if 'menu' not in endpoint:
                continue

            menu_cfg = endpoint['menu']
            menu_name = menu_cfg.pop('name')
            menu = blueLinks.setdefault(menu_name, {'description': '', 'menuItems': []})
            menu['menuItems'].append(menu_cfg)

        for menu in blueLinks:
            blueLinks[menu]['menuItems'].sort(key=cmp_to_key(lambda x, y: x['order'] - y['order']))

        return self.render_admin_template('admin/index.html', {
            'namespace'             : namespace,
            'breadcrumbs'           : self.generateBreadcrumbs(namespace),
            'blueLinks'             : blueLinks,
            'msgid'                 : kwargs.get('msgid', None)
        })


    @route('/:namespace/*endpoint_path/', methods=['GET'])
    @expose_page(must_login=True)
    @set_cache_level('never')
    def listEntities(self, namespace, endpoint_path, **kwargs):
        """
        displays a list of entities that exist at a given endpoint,
        provides management links to edit / delete / create entities.
        """

        action = kwargs.get('action', False)
        if endpoint_path.startswith('adddata') or endpoint_path.startswith('adddatamethods') or endpoint_path.startswith('data/inputs/') or endpoint_path.startswith('deployment/server/setup/data/inputs/'):
            # SPL-118358, The capability checks expects the 'adddata' endpoint and not 'adddata/addsource'
            # Same code as below so it should not have any new impact.
            if endpoint_path.startswith('adddata/'):
                endpoint_path = 'adddata'

            if endpoint_path.startswith('adddatamethods/'):
                endpoint_path = 'adddatamethods'

            # if it's an edit page (action=edit), then endpoint_path ends with the entity name that we can cut out
            # the remaining core path will be used to check against the manager endpoint in renderSHCErrorifEnabled
            endpoint_path_core = endpoint_path if action != 'edit' else endpoint_path[:endpoint_path.rfind('/')]
            res = self.renderSHCErrorifEnabled(endpoint_path_core)
            if res:
                return res

        # Hide explore data, virtual indexes and archives in cloud and light
        isExploreDataPage = endpoint_path.startswith('explore_data')
        isVirtualIndexPage = endpoint_path.startswith('virtual_indexes') or endpoint_path.startswith('vix_index_new') or endpoint_path.startswith('vix_provider_new')
        isArchivePage = endpoint_path.startswith('archive_new')
        if (util.isCloud() or util.isLite()) and (isVirtualIndexPage or isArchivePage or isExploreDataPage):
            raise cherrypy.HTTPError(404, _('Splunk cannot find "%s".') % (endpoint_path))

        # Show the archive management page only on product cloud.
        if endpoint_path.startswith('archive_management'):
            if not util.isCloud():
                raise cherrypy.HTTPError(404, _('Splunk cannot find "%s".') % (endpoint_path))

        # Redirect indexes to indexes_cloud if product is cloud. Both data/indexes and data/indexes_cloud will point to cloud indexes page.
        # Hide view single indexes. Redirect to listing page if _new page is reached.
        # Passing the url routing powers to backbone.js
        if endpoint_path.startswith('data/indexes'):
            if util.isCloud():
                if util.isLite():
                    endpoint_path = 'data/indexes/cloud/light'
                else:
                    endpoint_path = 'data/indexes/cloud'
            else:
               endpoint_path = 'data/indexes'
        elif endpoint_path.startswith('adddata/'):
            endpoint_path = 'adddata'
        elif endpoint_path.startswith('adddatamethods/'):
            endpoint_path = 'adddatamethods'
        elif endpoint_path.startswith('http-input/'):
            endpoint_path = 'http-input'

        # on cloud if dmc is enabled we show the new apps listing page instead of the old one. Old page can still
        # be accessed by adding a query parameter "redirect=false"
        if endpoint_path == "apps/local":
            if util.isCloud() and normalizeBoolean(kwargs.get('redirect', True)) and not _is_DMCDisabled():
                self.redirect_to_url("/app/dmc")

        # on cloud if dmc is enabled , we show the dmc app install page instead of the old one
        if endpoint_path == "appsremote":
            if util.isCloud() and not _is_DMCDisabled():
                self.redirect_to_url("/app/dmc/install_app")

        uiHelper = self.fetchUIHelpers(search=endpoint_path, namespace=namespace)

        viewTemplate = uiHelper.get('template')

        if viewTemplate:
            endpoint_path = endpoint_path.replace('/', '_') #flatten the path to be used as a view id
            return self.render_template(viewTemplate, {'app': None, 'page':endpoint_path, 'dashboard': '', 'splunkd': {}})

        # required to handle the conditional import in this method
        global splunk

        logger.debug('listEntities endpoint_path == %s' % endpoint_path)
        error = None
        entities = None
        entity = None
        form_defaults = None
        statusCode = 0
        nagwareMessaging = {}
        entity_name = None
        uiHelper_elements ={}

        noContainer = kwargs.get('noContainer', None)
        eleOnly = kwargs.get('eleOnly', None)


        search = kwargs.get('search', None)
        app_name = kwargs.get('app_name', None)

        defaultCount = 25
        defaultAppOnly = False
        sessionKey = cherrypy.session['sessionKey']
        count_arg = kwargs.get('count')
        requestedCount = count_arg if count_arg and count_arg.isnumeric() else None
        userPrefs = en.getEntity("data/user-prefs", "general", namespace="user-prefs")
        savedCount = userPrefs.get("eai_results_per_page")
        savedAppOnly = normalizeBoolean(userPrefs.get("eai_app_only"))
        newUserPrefs = {}
        hideBack = False
        isFree = normalizeBoolean(cherrypy.config["is_free_license"])
        appinstall = AppInstallController()
        isRestartRequired = appinstall.isRestartRequired()
        showAdvancedEdit = False

        if not isFree and not util.isLite() and endpoint_path.startswith('saved/searches'):
            try:
                en.getEntity('data/ui/manager', 'saved_searches_advancededit')
                showAdvancedEdit = True
            except splunk.ResourceNotFound as e:
                showAdvancedEdit = False

        # release the session lock as some resources can take a
        # long time to fetch (eg. those involving LDAP quries)
        cherrypy.session.release_lock()

        """
        logger.debug("\n" *5)
        logger.debug("*" * 20)
        logger.debug("rc: %s" % requestedCount)
        logger.debug("sc: %s" % savedCount)
        logger.debug("*" * 20)
        logger.debug("\n" *5)
        """

        if savedCount == None or ((requestedCount != None) and (requestedCount != savedCount)):
            if (savedCount == None) and (requestedCount == None):
                requestedCount = defaultCount
            logger.debug("User pref for results per page not set, or the request to change the saved setting came in.")
            newUserPrefs["eai_results_per_page"] = requestedCount
            count = requestedCount
        else:
            logger.debug("User pref for results per page is set to %s." % savedCount)
            count = savedCount

        offset = kwargs.get('offset', 0)
        sort_key = kwargs.get('sort_key', None)
        sort_dir = kwargs.get('sort_dir', None)
        sort_mode = kwargs.get('sort_mode', 'natural')
        msgid = kwargs.get('msgid', None)
        pwnr = kwargs.get('pwnr', None)
        app_only = normalizeBoolean(kwargs.get('app_only'))
        listable_only = False

        # SPL-48272 - Display message in time created order (desc)
        if endpoint_path == 'messages' and sort_key is None:
            sort_key = 'timeCreated_epochSecs'
            sort_dir = 'desc'

        # exposing peer health information requires a special url arg
        if endpoint_path == 'search/distributed/peers':
            kwargs['api.mode'] = 'extended'

        if savedAppOnly == None or ((app_only != None) and (app_only != savedAppOnly)):
            if (savedAppOnly == None) and (app_only == None):
                app_only = defaultAppOnly
            newUserPrefs["eai_app_only"] = app_only
        else:
            app_only = savedAppOnly
        kwargs['app_only'] = app_only

        if len(newUserPrefs) > 0:
            # set the user-pref
            saved, err = self.saveEntity("data/user-prefs", "general", newUserPrefs, namespace="user-prefs", entity_owner=auth.getCurrentUser()['name'], entity_uri=None )

        ns = kwargs.get('ns', namespace)
        if ns == "" or ns == None:
            # use the url part for the namespace... default app
            ns = namespace


        endpoint_base = endpoint_path
        appOptionList = []
        pwnrOptionList = []
        showNewButton = True
        # clone
        isCloneAction = False

        if action == "edit":
            #logger.debug("edit action of listEntities")
            segments = endpoint_path.split("/")
            entity_name = segments.pop()
            endpoint_base = "/".join(segments)

            defaultView = "create" if entity_name == en.NEW_EAI_ENTITY_NAME else "edit"
            viewFilterMode = kwargs.get('viewFilter', defaultView)
            # switch for uri or template for endpoint inference
            uri = kwargs.get('uri', None)
            template = kwargs.get('template', None)

            isNewEntity = (entity_name == en.NEW_EAI_ENTITY_NAME)

            if (isNewEntity and uri is not None):
                logger.debug("this is a clone. do some stuff differently.")
                isCloneAction = True

            if not uri and template:
                uri = en.buildEndpoint(endpoint_base, entityName=template, namespace=ns, owner=auth.getCurrentUser()['name'])

            if endpoint_path.startswith('deployment/server/setup/data/inputs/remote_monitor'):
                appName = {"app_name": app_name}
                uri += '?' + urllib_parse.urlencode(appName)

            # logger.debug("view filter mode:%s." % viewFilterMode)
            try:
                uiHelper, form_defaults, entity, statusCode, msgid = self.getSingleEntity(endpoint_path, uri=uri, namespace=ns, viewFilter=viewFilterMode)
            # exception for the HTTPRedirect to avoid bypassing original redirect in the first place!
            except cherrypy.HTTPRedirect:
                raise
            except splunk.AuthorizationFailed as ex:
                logger.error('AdminController.listEntities: Insufficient user permissions for "%s". Returning 404.' % cherrypy.request.path_info)
                raise cherrypy.HTTPError(404)
            except Exception as e:
                error = _('Splunk could not perform action for resource %(resource)s %(error)s') % {'resource':endpoint_base, 'error':e}

            if error:
                redirect_to = "/manager/%s/%s" % (namespace, endpoint_base)
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', '%s' % (error))
                kwargs['msgid'] = msgid
                return self.redirect_to_url(redirect_to, _qs={'msgid':msgid})

            # custom setup redirect logic SPL-103775
            eai_acl = entity.get('eai:acl')
            if eai_acl:
                app_name = eai_acl.get('app')
                output = cached.getEntities('apps/local', search=['disabled=false'], count=-1)
                app = output.get(app_name)
                setup_view = None
                requiresSetup = None
                if app:
                    setup_view = app.get('setup_view')
                    requiresSetup = app.getLink('setup') and app['configured'] == '0'
                if setup_view and requiresSetup:
                    logger.info("Redirecting to custom setup for app=%s view=%s" % (app_name, setup_view))
                    self.redirect_to_url(['app', app_name, setup_view])
                    return
            # Pull in any defaults that are set via the query string;
            # mask out any params that are ineligible for override
            # NOTE: see security notes in SPL-41608
            form_overrides = dict([i for i in kwargs.items() if i[0].startswith(QS_PROP_PREFIX)]
            )
            for key in form_overrides:
                if not any(map(key[len(QS_PROP_PREFIX):].startswith, QS_PROP_BLACKLIST)):
                    logger.info('Using override value from URL: %s=%s' % (key, form_overrides[key]))
                    form_defaults[key[len(QS_PROP_PREFIX):]] = form_overrides[key]


            if endpoint_base.startswith("apps/local/") and entity_name == "setup":
                app_name = eai_acl.get('app')
                if cached.isModSetup(app_name):
                    return self.redirect_to_url(['app', app_name, "mod_setup"], _qs={
                        "redirect_to_custom_setup": "1",
                        "action": "edit"
                    })
                hideBack = True # Don't display "return to <app>" link for app setup - SPL-36541
                if '_noInputElements' in uiHelper:
                    # hide save/cancel buttons when there are no input elements - SPL-37968
                    eleOnly = True

            elements = None
            if "elements" in uiHelper:
                elements = uiHelper['elements']
            self.flattenElements(elements, uiHelper_elements)

            try:
                entity_name = entity.name
            except (AttributeError):
                entity_name = None
            if isCloneAction:
                # for permissions
                try:
                    ptest = en.getEntities(endpoint_base, namespace=splunk.getDefault('namespace') if ns=='-' else ns, owner="-")
                    hasCreateLink = bool(any(filter((lambda x: x[0] == 'create'), ptest.links)))
                except Exception as e:
                    hasCreateLink = False
                    logger.warn('Got error response while checking for clone privileges: %s' % e)
                entity.isClonable = hasCreateLink

                new_required_fields = []
                try:
                    new_entity = en.getEntity(endpoint_base, en.NEW_EAI_ENTITY_NAME, namespace=ns, owner=auth.getCurrentUser()['name'])
                except Exception as e:
                    pass

                if isinstance(new_entity.get('eai:attributes'), dict) and isinstance(new_entity['eai:attributes'].get('requiredFields'), list):
                    new_required_fields = new_entity['eai:attributes'].get('requiredFields')

                kwargs['__clonesource'] = entity_name
                entity_name = "Clone from %s" % entity_name
                # reach into the entity and add name to the eai:attributes[requiredFields] list
                # logger.debug(entity.get('eai:attributes').get('requiredFields'))
                if isinstance(entity.get('eai:attributes'), dict) and isinstance(entity['eai:attributes'].get('requiredFields'), list):
                    required_fields = set(entity['eai:attributes']['requiredFields'])
                    required_fields.add('name')
                    required_fields.update(new_required_fields)
                    entity['eai:attributes']['requiredFields'] = list(required_fields)


                # logger.debug(entity.get('eai:attributes').get('requiredFields'))
                # logger.debug("*" * 50)

            breadcrumbs = self.generateBreadcrumbs(namespace, endpoint_base, entity_name)
            if 'breadcrumbs' in kwargs:
                # override beginning of breadcrumb chain from list supplied on the query string
                final2 = breadcrumbs[-2:]
                breadcrumbs = util.parse_breadcrumbs_string(kwargs['breadcrumbs'])
                breadcrumbs.append(final2[0]) # "Files & Directories"
                breadcrumbs.append(final2[1]) # "Add New"
        else:
            try:
                logger.debug("fetching ui helper for %s." % endpoint_path)
                breadcrumb_endpoint_path = None
                elems=endpoint_path.split("/")

                # TODO: FIXME
                # get deployment server working
                ###
                if len(elems) == 4 and elems[0]=='deployment' and elems[1]=='serverclass_status' and elems[3]=='status':
                    uiHelper = self.fetchUIHelpers( search='deployment/serverclass_status', namespace=namespace)
                    breadcrumb_endpoint_path = 'deployment/serverclass_status'
                else:
                    uiHelper = self.fetchUIHelpers(search=endpoint_path, namespace=namespace)
                #logger.debug("uiHelper:: %s" % uiHelper)

                # determine if this list show show a 'New' button or not
                elems = uiHelper.get('elements', [])
                if len(elems) > 0:
                    views = elems[0].get('showInView', [])
                    showNewButton = 'create' in views

                # determine if we should filter by app context
                showAppContext = normalizeBoolean(uiHelper.get('showAppContext', '0'))
                if not showAppContext:
                    app_only = False

                listable_only = normalizeBoolean(uiHelper.get('applyListFilter', '0'))

            except splunk.ResourceNotFound as e:
                raise cherrypy.NotFound()

            # pick up the application and owner lists
            try:
                appList = cached.getEntities("apps/local", search=['disabled=false'], count=-1)
                appOptionList = [  {'label': '%s (%s)' % (appList[x].get('label'), appList[x].name), 'value': appList[x].name} for x in appList ]

                if util.isLite():
                    newList = []
                    appWhitelist = []
                    appList = auth.getUserPrefsGeneral('app_list')
                    addonList = auth.getUserPrefsGeneral('TA_list')

                    if appList:
                        appList = appList.split(',')
                    else:
                        appList = ['search']

                    appWhitelist.extend(appList)

                    if addonList:
                        addonList = addonList.split(',')
                        appWhitelist.extend(addonList)

                    for entry in appOptionList:
                        if entry['value'] in appWhitelist:
                            newList.append(entry)

                        appOptionList = newList
            except Exception as e:
                logger.error("exception accessing apps local endpoint.")
                logger.exception(e)

            if not isFree:
                try:
                    # per SPL-24818 ... cap large user environments to 250... should we try to cache this or do something smarter here?
                    pwnrList = en.getEntities("authentication/users", count=250, search="roles=*")
                    pwnrOptionList = [  {'label': "%s (%s)" % (pwnrList[x]['realname'] or pwnrList[x].name, pwnrList[x].name), 'value': pwnrList[x].name} for x in pwnrList ]
                    pwnrOptionList.append({'label': _("No owner"), 'value': 'nobody'})
                except Exception as e:
                    logger.error("exception accessing users endpoint:")
                    logger.exception(e)

            if pwnr and pwnr != "-":
                owner_search = "eai:acl.owner=%s" % pwnr
                if not search:
                    search = owner_search
                elif isinstance(search, list):
                    search.append(owner_search)
                else:
                    search = [search, owner_search]

            if app_only and ns != '-':
                app_search = "eai:acl.app=%s" % ns
                if not search:
                    search = app_search
                elif isinstance(search, list):
                    search.append(app_search)
                else:
                    search = [search, app_search]

            if listable_only:
                listable_search = "eai:acl.can_list=1 OR eai:acl.removable=1"
                if not search:
                    search = listable_search
                elif isinstance(search, list):
                    search.append(listable_search)
                else:
                    search = [search, listable_search]

            try:
                logger.debug("getting entities list")
                args = {'namespace': ns, 'owner':'-', 'search':search, 'count':count, 'offset':offset, 'sort_key':sort_key, 'sort_dir':sort_dir, 'sort_mode':sort_mode, 'unique_key':'id', 'sessionKey':sessionKey}

                args.update( dict([(k[4:], kwargs[k]) for k in kwargs if k.lower().startswith('api.') and k[4:] not in args]) )
                entities = en.getEntities(endpoint_path, **args)
                if len(entities.messages) > 0:
                    msg = entities.messages[0]
                    msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push(msg['type'], msg['text'])
                # in this case we need to augment the entities with their endpoint_bases
                if endpoint_path == 'admin/directory':
                    kwargs['can_reassign'] = self.can_access_manager_xml_file('bulkreassign')
                    for e in entities.values():
                        location = e.get('eai:location')
                        if location is not None and len(location) > 0 and location[0] == '/':
                            # Strip leading URL sep.
                            location = location[1:]
                        e['endpoint_base'] = location

            except splunk.ResourceNotFound as e:
                raise cherrypy.HTTPError(404, _('Splunk cannot find "%s".')
                    % (endpoint_path))


            except splunk.InternalServerError as e:
                # when the params are stripped away (action=edit) this occurs and resulted in an ugly stack trace
                # this only happens if they manually type in an incorrect URL, or for some reason the params get removed
                # which was occuring from the logout.
                # catching and making them start over.
                logger.debug("500 internal server error.")
                logger.exception(e)
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', 'An error occurred completing this request: %s.' % e.get_message_text())
                # provide an empty entities so we can get to teh list view page.
                entities = {}

            except splunk.RESTException as e:
                entities = {}
                logger.exception(e)
                statusCode = e.statusCode
                errMsgStr = e.get_message_text() or _("An exception occurred, but no message string was extracted.")
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', '%s' % errMsgStr )

            except splunk.SplunkdConnectionException as e:
                entities = {}
                logger.exception(e)
                errMsgStr = "Timed out while waiting for splunkd daemon to respond (%s). Splunkd may be hung." % str(e)
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', '%s' % errMsgStr )

            breadcrumbs = self.generateBreadcrumbs(namespace, breadcrumb_endpoint_path or endpoint_path)
            if 'breadcrumbs' in kwargs:
                final = breadcrumbs[-1]
                breadcrumbs = util.parse_breadcrumbs_string(kwargs['breadcrumbs'])
                breadcrumbs.append(final)

        if statusCode == 402:

            #hows about some nagware to go along with this error message?
            if "menu" in uiHelper:
                endpoint_label = uiHelper['menu']['label']
            elif uiHelper.get('breadcrumb_entityname'):
                endpoint_label = uiHelper['breadcrumb_entityname']
            elif uiHelper.get('breadcrumb_name'):
                endpoint_label = uiHelper['breadcrumb_name']
            elif 'header' in uiHelper:
                endpoint_label = uiHelper['header']
            else:
                self.deny_access()

            endpoint_description = uiHelper.get("menu", {}).get("description")


            return self.render_admin_template('admin/402.html', {
                'feature'             : endpoint_label
            })

        if noContainer:
            returnTemplate = 'admin/edit.html'
        else:
            returnTemplate = 'admin/index.html'

        uiHelper = DynamicUIHelper(uiHelper, __context={'form_defaults':form_defaults, 'entity_name':entity_name, 'namespace':namespace}) if uiHelper and len(uiHelper) !=0 else uiHelper

        # As we are transitioning to backbone pages, we need to block the user
        # from accessing the old python page by hardcoding the URL.
        # Assume that if the template is defined,
        # then the user should not see the original python page,
        # and instead be redirected to the template.

        if 'template' in uiHelper:
            endpoint_base = endpoint_base.replace('/', '_') #flatten the path to be used as a view id
            return self.render_template(uiHelper['template'], {'app': None, 'page':endpoint_base, 'dashboard': '', 'splunkd': {}})

        if not noContainer and len(uiHelper) == 0:
            raise cherrypy.HTTPError(404)

        return self.render_admin_template(returnTemplate, {
            'namespace'     : namespace,
            'breadcrumbs'   : breadcrumbs,
            'entities'      : entities,
            'entity'        : entity,
            'form_defaults' : form_defaults,
            'entity_name'   : entity_name,
            'isCloneAction' : isCloneAction,
            'uiHelper'      : uiHelper,
            'uiHelper_elements'      : uiHelper_elements,
            'showNewButton' : showNewButton,
            'endpoint_path' : endpoint_path,
            'endpoint_base' : endpoint_base,
            'error'         : error,
            'installUrl'    : INSTALL_URL,
            'nagwareMessaging'         : nagwareMessaging,
            'eleOnly'       : eleOnly,
            'kwargs'        : kwargs,
            'appOptionList' : appOptionList,
            'pwnrOptionList': pwnrOptionList,
            'msgid'         : msgid,
            'isRestartRequired' : isRestartRequired,
            'showAdvancedEdit': showAdvancedEdit
        }, hide_back=hideBack)

    @route('/:namespace/*endpoint_base/:element=_element/:element_name', methods=['POST', 'GET'])
    @expose_page(must_login=True)
    def fetch_element(self, namespace, endpoint_base, element, element_name, form_defaults=None, element_overlay=None, entity_name=None, eai_attributes=None):
        """
        Fetch an individual form element for an enedpoint using the supplied form values
        Optionally update the element's uiHelper configuration using element_overlay
        """
        uiHelper = self.fetchUIHelpers(search=endpoint_base, namespace=namespace)
        if len(uiHelper) == 0:
            logger.error('listProperties - unable to load the section form definition for endpoint=%s' % endpoint_base)
            raise cherrypy.NotFound("Endpoint/element not found")

        form_defaults = json.loads(form_defaults) if form_defaults else {}
        element_overlay = json.loads(element_overlay) if element_overlay else {}
        eai_attributes = json.loads(eai_attributes) if eai_attributes else {}

        uiHelper_flatten = {}
        self.flattenElements(uiHelper['elements'], uiHelper_flatten)
        # deep_update_dict modifies in place; deep copy in case this data ends up being cached in the future which would
        # probably lead to a hard to track down bug
        element = copy.deepcopy(uiHelper_flatten[element_name])
        # remove dynamicOptions to prevent arbitraty python code execution via this endpoint
        if 'dynamicOptions' in element_overlay:
            element_overlay.pop('dynamicOptions', None)
        util.deep_update_dict(element, element_overlay)

        # NOTE: this intentionally calls render_template(), not render_admin_template()
        return self.render_template('admin/element.html', {
            'form_defaults' : form_defaults,
            'eaiAttributes' : eai_attributes,
            'form_errors': {},
            'element': DynamicUIHelper(element, __context={'form_defaults':form_defaults, 'entity_name':entity_name, 'namespace':namespace})
        })


    @route('/:action=permissions/:namespace/*endpoint_path', methods=['POST', 'GET'])
    @expose_page(must_login=True)
    @set_cache_level('never')
    def permissions(self, namespace, action, endpoint_path, sharing='user', owner=None, perms_read=None, perms_write=None, **kwargs):
        '''
        Permission control for an endpoints ACL.

        Arg:
        namespace: The user namespace.
        action: The hard-coded route beginning pattern of 'permissions/'
        endpoint_base: A valid endpoint to perform ACL actions on (ie., /search/saved/foo%wbar)
        sharing: One of 'app' or 'global' or defaults to 'user'. Sharing is caring.
        owner: The owner of the object or defaults to None.
        perms_read: A string or list of read owners/roles, defaults to None.
        perms_write: A string or list of write owners/roles, defaults to None
        '''
        # app
        app = None
        # the cancel button url
        cancel_url = self.getCancelURL(namespace, **kwargs)
        # place holder for error messages
        error = None
        # place holder for general info messages
        info = None
        # acl entity
        entity = None
        # the permission entity name
        entity_name = 'acl'
        # active selected roles
        active_roles = []
        # modifiable setting
        modifiable = None
        # object label (eg., Saved Search, etc...)
        object_label = 'Object'
        # object label mapping (eg., /saved/searches/foobar is saved->Saved Search)
        object_labels = {
            'saved/eventtypes': _('saved eventtype'),
            'saved/searches': _('saved search'),
            'data/ui/views': _('views'),
            'commands/': _('search commands and scripts'),
        }

        # control if redirect on successful post operation
        redirect_success = True
        # admin dependencies
        uiHelper = {
            'header': object_label,
            'elements': []
        }

        segments = endpoint_path.split("/")
        entity_name = segments.pop()
        endpoint_base = "/".join(segments)

        # fully qualified action path
        action_path = ''

        # derive the object label
        for object_label_key in object_labels:
            if endpoint_base.find(object_label_key)>-1:
                object_label = object_labels[object_label_key]
                break

        post_error = None

        # handle POST methods
        if cherrypy.request.method == 'POST':
            # add set entity routine
            entityUpdate = None
            entityBoilerPlate = None
            try:
                entityBoilerPlate = en.Entity(endpoint_base, entity_name, namespace=namespace, owner=splunk.auth.getCurrentUser()['name'])
            except Exception as e:
                post_error = _('Splunk could not update permissions for resource %(resource)s %(error)s') % {'resource':endpoint_base, 'error':e}
            # if we have an update entity object proceed
            if entityBoilerPlate:
                # update entity boiler plate sharing properties
                entityBoilerPlate.properties['sharing'] = sharing
                # update entity boiler plate owner properties
                entityBoilerPlate.properties['owner'] = owner
                # update entity boiler plate read perms properties
                if perms_read:
                    # single read perms
                    if isinstance(perms_read, unicode):
                        entityBoilerPlate.properties['perms.read'] = perms_read
                    # multiple read perms
                    elif isinstance(perms_read, list):
                        entityBoilerPlate.properties['perms.read'] = ",".join(perms_read)
                # update entity boiler plate write perms properties
                if perms_write:
                    # single write perms
                    if isinstance(perms_write, unicode):
                        entityBoilerPlate.properties['perms.write'] = perms_write
                    # multiple write perms
                    elif isinstance(perms_write, list):
                        entityBoilerPlate.properties['perms.write'] = ",".join(perms_write)
                # update permissions for this resources ACL
                try:
                    result = en.setEntity(entityBoilerPlate, uri=_extractPathFromURI(kwargs['uri']) + '/acl')
                except Exception as e:
                    post_error = _('Splunk could not update permissions for resource %(resource)s %(error)s') % {'resource':endpoint_base, 'error':e}
                # log properties that could not be saved
                if post_error:
                    logger.error('Splunk could not update ACL with the following params: %s' % entityBoilerPlate.properties)
            # set proper info message for successful update of permissions
            if post_error is None:
                return self.redirect_to_url(cancel_url)

        #gracefully handle entity retrieval
        try:
            entity_name = cpUnquoteEntity(entity_name)
            entity = en.getEntity(endpoint_base, entity_name, uri=_extractPathFromURI(kwargs.get('uri', None)), namespace=namespace, owner=auth.getCurrentUser()['name'])
            entity_name = entity.name
        except Exception as e:
            error = _('Splunk could not retrieve permissions for resource %(resource)s %(error)s') % {'resource':endpoint_base, 'error':e}

        if error:
            redirect_to = "/manager/%s/%s" % (namespace, endpoint_base)
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', '%s' % (error))
            kwargs['msgid'] = msgid
            return self.redirect_to_url(redirect_to, _qs={'msgid':msgid})

        # if no error and entity acl data exists perform standard routines
        if error is None and entity['eai:acl']:
            # if acl and perms related data present:
            # 1) build an ordered list of active roles
            # 2) assign perms_read and perms_write list

            if entity['eai:acl']['perms']:
                active_roles = self._permissionsGetRoles(entity['eai:acl']['perms'])
                perms_read = entity['eai:acl']['perms'].get('read', [])
                perms_write = entity['eai:acl']['perms'].get('write', [])
                # set perms to empty list if None exist
            else:
                perms_read = []
                perms_write = []
                # set acl modifiable state if present

            if entity['eai:acl']['modifiable']:
                try:
                    modifiable = normalizeBoolean(entity['eai:acl']['modifiable'], enableStrictMode=True)
                except Exception as e:
                    logger.warn("Splunk could not normalize acl modifiable value to Boolean %s" % e)
                sharing = entity['eai:acl']['sharing']
                owner = entity['eai:acl']['owner']
                app = entity['eai:acl']['app']

        # available sharing options
        sharing_options = []

        if entity['eai:acl'].get('sharing') == 'user' or entity['eai:acl'].get('can_share_user', None) == '1':
            if util.isLite():
                sharing_options.append(('user', _('Private')))
            else:
                sharing_options.append(('user', _('Keep private')))

        if entity['eai:acl'].get('sharing') == 'app' or entity['eai:acl'].get('can_share_app', None) == '1':
            if not util.isLite():
                app_label = _('This app only (%s)') % (entity['label'] if app == 'system' else app)
                sharing_options.append(('app', app_label))

        if entity['eai:acl'].get('sharing') == 'global' or entity['eai:acl'].get('can_share_global', None) == '1':
            if util.isLite():
                sharing_options.append(('global', _('Shared')))
            else:
                sharing_options.append(('global', _('All apps (system)')))

        # set appropriate modifiable message
        if modifiable is False:
            info = _('Permissions for this object are restricted. It is not modifiable.')
        # get roles
        role_entities = None
        roles = []
        try:
            role_entities = en.getEntities('authorization/roles', namespace=namespace, count=-1)
        except Exception as e:
            pass
        # create list of roles
        if role_entities:
            roles = [x for x in role_entities]

        breadcrumbs = self.generateBreadcrumbs(namespace, endpoint_base, entity_name, kwargs.get('uri', None), 'Permissions')
        isFree = normalizeBoolean(cherrypy.config["is_free_license"])

        return self.render_admin_template('admin/permissions.html', {
            'namespace' : namespace,
            'breadcrumbs' : breadcrumbs,
            'error': error or post_error,
            'info': info,
            'entity': entity,
            'active_roles': active_roles,
            'modifiable': modifiable,
            'sharing': sharing,
            'owner': owner,
            'app': app,
            'perms_read': perms_read,
            'perms_write': perms_write,
            'action_path': action_path,
            'object_label': object_label,
            'sharing_options': sharing_options,
            'perms_modifiable': entity['eai:acl'].get('can_change_perms', None) == '1',
            'roles': roles,
            'cancel_url': self.make_url(cancel_url),
            # required admin template args
            'entity_name' : "Permissions",
            'endpoint_base' : endpoint_base,
            'form_defaults' : entity,
            'uiHelper' : uiHelper
        })


    def _permissionsGetRoles(self, perms):
        """
        Get an ordered list of roles from an ACL's perms.

        Arg:
        perms: A dictionary of permissions associated to a list of roles.
        """
        unique_rolls = []
        for roles in perms.values():
            unique_rolls.extend(roles)
        return sorted(set(unique_rolls))

    def renderSHCErrorifEnabled(self, page=None):
        """
        Checks if SHC mode is on and renders a wall page if true
        If optional page arg is specified, prevents the wall page if the page is accessible
        """
        isFree = normalizeBoolean(cherrypy.config["is_free_license"])
        if isFree:
            return None

        try:
            shc_mode = False
            """
            SPL-118358, shcluster/config endpoint requires ' list_search_head clustering' capability, so it cannot be
            accessed by a user whose role that doesn't have the capability. Use a different endpoint to determine if
            the node is a part of a SH cluster.
            """
            (sr_response, sr_content) = rest.simpleRequest(SERVER_ROLES_ENDPOINT)
            sr_content_json = json.loads(sr_content)
            roleList = sr_content_json['entry'][0]['content']['role_list']
            for role in roleList:
                if role == SHC_MEMBER or role == SHC_CAPTAIN:
                    shc_mode = True
                    break
        except (splunk.LicenseRestriction, splunk.AuthorizationFailed) as e:
            # Throws a 402 in Splunk Light.
            return None

        # Check to see if SHC is enabled. If so, render the wall.
        if shc_mode:
            if page and self.can_access_manager_xml(page):
                return None

            return self.render_admin_template('admin/notavailable.html', {
                'title'               : _('Action is not available'),
                'reason'              : _('Current instance is running in SHC mode and is not able to add new inputs'),
                'helpLocation'        : 'manager.shclustering.settings.menu'
            })

        return None



    @route('/:namespace/*endpoint_base/:action=move', methods=['GET', 'POST'])
    @expose_page(must_login=True)
    def moveObject(self, namespace, endpoint_base, action, **kwargs):
        newns = kwargs.pop('newns')
        entname = kwargs.pop('name')
        entowner = kwargs.pop('entowner')
        uri = _extractPathFromURI(kwargs.pop('uri'))
        kwargs.pop('splunk_form_key')
        pwnr = entowner

        postArgs = {
            "app" : newns,
            "user" : pwnr
        }

        if endpoint_base.startswith('data/props/extractions'):
            postArgs.setdefault('safe_encoding', 1)

        try:
            serverResponse, serverContent = rest.simpleRequest(uri, postargs=postArgs, method='POST', raiseAllErrors=True)
            msg = ("info", "Successfully moved '%s' to '%s'." % (entname, newns))
        except splunk.RESTException as e:
            msg = ("error", e.get_message_text())

        msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push(msg[0], msg[1])
        kwargs['msgid'] = msgid
        kwargs['ns'] = newns

        redirect_to = "/manager/%s/%s" % (namespace, endpoint_base)

        return self.redirect_to_url(redirect_to, _qs=kwargs)

    @route('/:namespace/*endpoint_base/:multiaction=multidelete', methods=['POST'])
    @expose_page(must_login=True)
    def multiaction(self, namespace, endpoint_base, multiaction, **kwargs):


        control = kwargs.pop('ctrl')
        control_link = kwargs.pop('ctrl_link')
        control_name = kwargs.pop('ctrl_name')
        showAppContext = False
        if 'showAppContext' in kwargs:
            showAppContext = kwargs.pop('showAppContext')

        if kwargs.get('redirect_to') != None:
            redirect_to = kwargs.pop('redirect_to')
            logger.debug("will redirect to %s." % redirect_to)
        else:
            redirect_to = "/manager/%s/%s" % (namespace, endpoint_base)

        if endpoint_base.startswith('data/props/extractions'):
            qs = {'safe_encoding' : 1}
            control_link += '?' + urllib_parse.urlencode(qs)

        logger.debug("perparing to %s %s." % (control, control_name) )
        try:
            en.controlEntity(control, control_link)
            if showAppContext:
                # used for messaging what app context the action was applied to
                control_parts = control_link.split("/")
                context_part = control_parts[3]
                msgStr = CONTROL_ACTIONS[control]['withcontext'] % {'item': control_name, 'context': context_part}
            else:
                if control_link == '/services/messages/restart_required' and control == 'remove':
                    msgStr = _('Restart message removed.')
                else:
                    msgStr = CONTROL_ACTIONS[control]['nocontext'] % {'item': control_name}
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('info', msgStr)
            kwargs['msgid'] = msgid
        except Exception as e:
            # logger.debug(e)
            errMsg = ": %s" %e
            # logger.debug("errMsg: %s" % errMsg)
            try:
                errMsg = e.get_message_text()
            except splunk.ResourceNotFound as e:
                errMsg = " Resource not found: %s" % e
            except:
                errMsg = ""
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', 'Error occurred attempting to %s %s: %s.' % (control, control_name, errMsg))
            kwargs['msgid'] = msgid

        # push changes if updating a forwarded input
        if control_link.startswith('/services/deployment/server/setup/data/inputs/'):
            uri = '/services/deployment/server/config/_reload'

            # figure out server class name from the url arg; update all if failed to extract
            postargs = {}
            serverClass = '*'
            qs = urllib_parse.urlsplit(control_link).query
            if len(qs) > 0:
                qs_args = urllib_parse.parse_qs(qs)
                app_name = qs_args.get('app_name')
                if app_name:
                    serverClass = app_name[0].replace('_server_app_', '')
                    postargs = {'serverclass': serverClass}

            try:
                serverResponse, serverContent = rest.simpleRequest(uri, postargs=postargs, method="POST")
            except Exception as e:
                logger.error('Failed to reload server class: %s' % serverClass)
                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', _('Failed to reload server class: %s.') % serverClass)
                kwargs['msgid'] = msgid

        kwargs.pop('splunk_form_key', None)

        return self.redirect_to_url(redirect_to, _qs=kwargs)


    def _processRepeatable(self, form_data, uiHelper_elements):
        """
        Find all elements in uiHelper that are repeatable and add fake
        uiHelper entries for all entries in form_data that match the repeatable's prefix
        This allows things like submitValueEdit to apply to dynamically created repeatable elements
        """
        for elname, helper in list(uiHelper_elements.items()): # We wrapped the call to items() in a list() for py3's dictionary changed size during iteration.
            if helper.get('type') != 'repeatable':
                continue
            prefix = helper.get('fieldprefix')
            ignoreprefix = helper.get('ignoreprefix')
            pattern = helper.get('fieldprefixregex')
            if not prefix:
                logger.error('repeatable element didn\'t define a prefix')
                continue
            if pattern:
                regex = re.compile(pattern)
                if not regex:
                    logger.error("repeatable failed to compile field regex pattern: %s" % pattern)
                    continue
            else:
                regex = None
            for elname in form_data:
                if not ((regex and regex.match(elname)) or (prefix and elname.startswith(prefix))):
                    continue
                if ignoreprefix and elname.startswith(ignoreprefix):
                    continue
                uiHelper_elements[elname] = helper['element']


    @route('/:namespace/*endpoint_base/:entity_name', methods=['POST'])
    @expose_page(must_login=True)
    def editProperties(self, namespace, endpoint_base, entity_name, **kwargs):
        """
        provide a location for posting entity updates
        a form for editing the properties of a given endpoint and entity
        test for editing properties.
        """
        fetchStaticHelpers = True
        error = None
        form_errors = {}
        form_defaults = {}
        uiHelper = {}
        redirectTail = kwargs.get("spl-ctrl_redirectionPath", endpoint_base)
        redirectToUrl = ['manager', namespace, redirectTail]
        redirectQSName = kwargs.get("spl-ctrl_redirectQSName", "redirecting")
        redirectQSVal = kwargs.get("spl-ctrl_redirectQSVal", "true")

        if sys.version_info < (3, 0):
            entity_name = entity_name.encode('utf-8')

        entity_name = urllib_parse.unquote(urllib_parse.unquote(entity_name))

        if endpoint_base.startswith("apps/local/") and entity_name == "setup":
            fetchStaticHelpers = False

        if fetchStaticHelpers:
            uiHelper = self.fetchUIHelpers(search=endpoint_base, namespace=namespace)
        else:
            # can't generate uiHelper without the entity, don't need it right here though.
            uiHelper = {}

        if len(uiHelper) == 0:
            logger.error('editProperties - unable to load the section form definition for endpoint=%s' % endpoint_base)


        showAppContext = uiHelper.get("showAppContext")
        editForm = formencode.Schema()
        editForm.allow_extra_fields = True

        entity_namespace = kwargs.pop('__ns', namespace)
        entity_owner     = kwargs.pop('__owner', '')
        entity_uri       = kwargs.pop('__uri', None)
        do_redirect      = kwargs.pop('__redirect', '0') == '1' and uiHelper.get('redirect')
        action           = kwargs.pop('__action', None)

        endpoint_base_saveto = kwargs.get('spl-ctrl_endpoint_override', endpoint_base)
        #logger.debug("endpoint_base_saveto: %s" % endpoint_base_saveto)

        if endpoint_base_saveto.startswith('deployment/server/setup/data/inputs') and not endpoint_base_saveto == 'deployment/server/setup/data/inputs/tcp/cooked':
            entity_uri = entity_uri[entity_uri.find('/deployment/server/setup/'):]

        try:
            saved = False
            uiHelper_elements = {}
            self.flattenElements(uiHelper.get('elements', []), uiHelper_elements)
            form_data = editForm.to_python(kwargs)
            # find repeatable template options
            self._processRepeatable(form_data, uiHelper_elements)
            for key, value in list(form_data.items()): #  We wrapped the call to items() in a list() for py3's dictionary changed size during iteration.
                # some values need to be post-processed prior to submission
                if key in uiHelper_elements:
                    submitValue = 'submitValueAdd' if entity_name==en.NEW_EAI_ENTITY_NAME else 'submitValueEdit'
                    p = uiHelper_elements[key].get(submitValue)
                    if p:
                        try:
                            nv = eval(p.strip())
                            if nv is None:
                                # allow submitValue* directive to prevent
                                # values from being submitted
                                del form_data[key]
                            else:
                                form_data[key] = nv
                        except Exception as e:
                            logger.exception('uiHelper %s operator failed for endpoint_base=%s entity_name=%s elementName=%s: %s' % (submitValue, endpoint_base, entity_name, key, str(e)))

                    if uiHelper_elements[key].get('type') == 'password':
                        password_confirm = form_data.get('spl-ctrl_%s-confirm' % key)
                        if ((password_confirm != '' and form_data[key] != password_confirm) or
                            (password_confirm == '' and form_data[key] not in ['', '********'])):
                            return self.render_json({
                                'status': 'FIELD_ERRORS',
                                'fields': { key: 'Passwords do not match' },
                                },
                                set_mime='text/html' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                            )

            # SPL-104439 - If creating a clone, disallow creation if the source name is the same as the target name
            if action == 'clone':
                objectName = form_data.get('name')
                sourceName = kwargs.get('__clonesource')
                if objectName == sourceName:
                    feedbackMessage = _('Cannot clone "%s". Object with this name already exists.') % objectName
                    logger.debug(feedbackMessage)
                    return self.render_json({
                        'status': 'ERROR',
                        'msg': feedbackMessage,
                        },
                        set_mime='text/plain'
                    )
            # if cloning a savedsearch (has vsid), clone the viewstate first and replace the vsid in the form with the new
            if action == 'clone' and form_data.get('vsid'):
                viewstate_hash = form_data.get('vsid')
                view_id, old_vsid = viewstate.parseViewstateHash(viewstate_hash)
                view_id = view_id or '*'
                current_user = cherrypy.session['user'].get('name')
                try:
                    vs = viewstate.get(view_id, old_vsid, namespace, current_user)
                    viewstate.clone(vs)
                    form_data['vsid'] = viewstate.buildStanzaName(view_id, vs.id)
                except Exception as e:
                    feedbackMessage = _('Encountered the following error while trying to clone: %s') % str(e)
                    logger.debug("feedbackMessage: %s." % feedbackMessage)
                    return self.render_json({
                        'status': 'ERROR',
                        'msg': feedbackMessage,
                        },
                        set_mime='text/plain' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                    )

            # Attempt to save the entity
            # Saving entities can take a long time (eg. for file upload)
            # Release the session lock before proceeding
            cherrypy.session.release_lock()
            saved, err = self.saveEntity(endpoint_base_saveto, entity_name, form_data, entity_namespace, entity_owner, entity_uri)
            if err:
                logger.debug("Error returned was: %s" % err)

            # for messaging
            savedName = form_data.get("name") if entity_name==en.NEW_EAI_ENTITY_NAME else entity_name
            if not savedName:
                if endpoint_base_saveto=='data/inputs/monitor' and entity_name==en.NEW_EAI_ENTITY_NAME and form_data.get('spl-ctrl_switcher')=='uploadfile':
                    # name is empty for uploaded files
                    savedName = _('Uploaded file')

            if saved:
                if entity_name == en.NEW_EAI_ENTITY_NAME:
                    actionApplied = _('saved')
                else:
                    actionApplied = _('updated')

                # logger.debug("err type: %s" % type(err))
                appendMsg = ""
                if (isinstance(err, dict)):
                    if len(err["messages"]) > 0:
                        appendMsg = err["messages"][0].get("text")
                        if not appendMsg.endswith("."):
                            appendMsg = appendMsg + "."

                # special casing for the weird tcp forwarding endpoint where name is always __default, so use the servers field instead
                if (endpoint_base=='data/outputs/tcp/group' and form_data.get('servers') != None):
                    savedName = form_data.get("servers")

                # When editing a forwarded input, push the serverclass reload so that app is propagated to fwders
                if endpoint_base.startswith('deployment/server/setup/data/inputs/'):
                    uri = '/services/deployment/server/config/_reload'
                    serverClass = kwargs.get('app_name', '').replace('_server_app_', '')
                    postargs = {'serverclass':serverClass}
                    try:
                        serverResponse, serverContent = rest.simpleRequest(uri, postargs=postargs)
                    except Exception as e:
                        logger.error('Failed to reload server class')

                if showAppContext:
                    # TRANS: "Successfully saved  mysearch in Search"
                    feedbackMessage = _('Successfully %(verb)s "%(noun)s" in %(namespace)s. %(extra)s') % {'verb':actionApplied, 'noun':savedName, 'namespace':entity_namespace, 'extra': appendMsg}
                else:
                    # TRANS: "Succesfully updated settings"
                    feedbackMessage = _('Successfully %(verb)s "%(noun)s". %(extra)s') % {'verb':actionApplied, 'noun': savedName, 'extra': appendMsg}

                msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('info', feedbackMessage)
                if do_redirect:
                    qs={ 'msgid':msgid, 'ns': entity_namespace, redirectQSName: redirectQSVal }

                    if endpoint_base == "apps/local" and util.isCloud() and not _is_DMCDisabled():
                        qs["redirect"] = False

                    # re-apply any search filters that were in place
                    for key in kwargs:
                        if key.startswith('__f_'):
                            qs[key[4:]] = kwargs[key]
                    redirect_target = self.make_url(redirectToUrl, _qs=qs)
                else:
                    redirect_target = ''

                ctrl_redirect_override = kwargs.get("spl-ctrl_redirect_override")
                #override all the other redirect values if redirect_override is passed in.
                if ctrl_redirect_override and _is_redirect_safe(ctrl_redirect_override):
                    logger.debug("Because this redirect can go to an arbitrary page, we log the feedback message instead of passing it along.")
                    logger.info(feedbackMessage)
                    redirect_target = ctrl_redirect_override

                return self.render_json({
                    'status': 'OK',
                    'msg': feedbackMessage,
                    'redirect': redirect_target
                    },
                    set_mime='text/plain' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                )

            else:
                if entity_name == en.NEW_EAI_ENTITY_NAME:
                    feedbackMessage = _('Encountered the following error while trying to save: %s') % err
                else:
                    feedbackMessage = _('Encountered the following error while trying to update: %s') % err

                logger.debug("feedbackMessage: %s." % feedbackMessage)
                return self.render_json({
                    'status': 'ERROR',
                    'msg': feedbackMessage,
                    },
                    set_mime='text/plain' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                )

        except formencode.api.Invalid as e:
            logger.debug("Form data failed validation, returning errors.")
            if e.error_dict:
                # return a mapping of field names to error messages
                return self.render_json({
                    'status': 'FIELD_ERRORS',
                    'fields': e.error_dict,
                    },
                    # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                    set_mime='text/html'
                )
            else:
                error = e.msg
                return self.render_json({
                    'status': 'ERROR',
                    'msg': e.msg,
                    },
                    set_mime='text/html' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
                )


    ############################################################################
    #
    #                      INTERSTITAL PAGES
    #
    ############################################################################

    @route('/:namespace/:action=datainputstats')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def datainputstats(self, namespace, action, operation=None, **kwargs):
        res = self.renderSHCErrorifEnabled('datainputstats')
        if res:
            return res

        if not self.can_access_manager_xml('datainputstats'):
            logger.error('Cannot reach endpoint "datainputstats". Insufficient user permissions.')
            raise cherrypy.HTTPError(404)

        endPointList = {
            'monitor'           : 'data/inputs/monitor',
            'tcp'               : 'data/inputs/tcp/raw',
            'udp'               : 'data/inputs/udp',
            'scripts'           : 'data/inputs/script',
            'http'              : 'data/inputs/http',
            'fwdMonitor'        : 'deployment/server/setup/data/inputs/remote_monitor',
            'fwdTcp'            : 'deployment/server/setup/data/inputs/tcp/remote_raw',
            'fwdUdp'            : 'deployment/server/setup/data/inputs/remote_udp',
            'fwdScripts'        : 'deployment/server/setup/data/inputs/remote_script',
            'fwdEventLogs'      : 'deployment/server/setup/data/inputs/remote_eventlogs',
            'fwdPerfMon'        : 'deployment/server/setup/data/inputs/remote_perfmon'
        }

        showWinInputs = True if sys.platform.startswith('win') else False
        if showWinInputs:
            winEndPointList = {
                'win-wmi'          : 'data/inputs/win-wmi-collections',
                'win-event-log'    : 'data/inputs/win-event-log-collections',
                'win-regmon'       : 'data/inputs/registry',
                'win-admon'        : 'data/inputs/ad',
                'win-perfmon'      : 'data/inputs/win-perfmon'
            }
            endPointList.update(winEndPointList)

        indexData = {} # provide overview data for data inputs, links to create new etc.
        canView = {} # permissions if user can view row

        # Populate indexData, canView and canCreate
        for (name, endPoint) in endPointList.items():
            try:
                if name == 'http':
                    # SPL-138551 http inputs are namespace-sensitive now
                    entities = en.getEntities(endPoint)
                else:
                    entities = en.getEntities(endPoint, namespace=namespace)
                canView[name] = self.can_access_manager_xml(endPoint)
            except splunk.AuthorizationFailed:
                entities = None
                canView[name] = False

            indexData[name] = 'N/A' if entities is None else entities.totalResults

            # Special case for http inputs. Getting entities from 'data/inputs/http' but page is http-eventcollector.
            # If entities can be fetched, it cannot be None (doesn't hit exception block) and assume user has permission.
            if entities is not None and name == 'http':
                canView[name] = True

        # Get modular inputs
        modInputs = []

        builtin_mod_inputs = self.builtin_mod_inputs()
        builtin_mod_inputs_ignore = []

        for key in builtin_mod_inputs:
            winput = builtin_mod_inputs[key]
            if winput['ignore']:
                # Both WinHostMon and MonitorNoHandle is having same title. Using description to filter MonitorNoHandle
                if key == 'MonitorNoHandle':
                    builtin_mod_inputs_ignore.append('description != "' + winput['input_description'] + '"')
                else:
                    builtin_mod_inputs_ignore.append('title != "' + winput['input_title'] + '"')

        try:
            offset = int(kwargs.get('offset', 0))
            count = int(kwargs.get('count', 10))
            search = " AND ".join(builtin_mod_inputs_ignore)
            modInputTypes = en.getEntities('data/modular-inputs', search=search, count=count, offset=offset, namespace=namespace)
            totalResults = modInputTypes.totalResults
            for (key, val) in modInputTypes.items():
                # Defaults for all modinputs
                can_view_mod_input = True
                endPoint = 'data/inputs/' + key
                mod_input_name = key
                mod_input_type = key
                add_as_mod_input = True

                # Builtin modinput specialization
                if key in builtin_mod_inputs:
                    winput = builtin_mod_inputs[key]
                    if winput['ignore']:
                        continue

                    endPoint = winput['endpoint']
                    mod_input_name = winput['input_name']
                    mod_input_type = winput['input_type']
                    add_as_mod_input = winput['add_as_mod_input']

                    # Shortcut: If we have manager xml to check, we can balk
                    # on authorization errors before we do the getEntities call below.
                    if winput['has_manager_xml']:
                        if not self.can_access_manager_xml(winput['endpoint']):
                            continue

                entities = en.getEntities(endPoint)

                # Don't show it if the user can't edit it.
                writeable = False
                for ln in entities.links:
                    if ln[0] == 'create':
                        writeable = True
                if not writeable:
                    continue

                itemCount = len(entities)
                inputTitle = val.properties['title']
                description = val.properties['description'] if val.properties['description'] else ''
                newInput = {
                    'name' : mod_input_name,
                    'type' : mod_input_type,
                    'title' : inputTitle,
                    'description' : description,
                    'count' : itemCount,
                    'add_as_mod_input': add_as_mod_input
                }
                modInputs.append(newInput)
        except splunk.AuthorizationFailed:
            modInputTypes = {}

        isForwardReceiveAccessible = self.can_access_manager_xml('forwardreceive')

        isDMCDisabled = _is_DMCDisabled()
        if not isDMCDisabled:
            dmcEndPointList = {
                'http'           : 'dmc/config/inputs/-/http'
            }
            for (name, endPoint) in dmcEndPointList.items():
                cnt = self.count_dmc_inputs(endPoint)
                indexData[name] = _('N/A') if cnt is None else cnt

        return self.render_admin_template('admin/datainputstats.html', {
            'isForwardReceiveAccessible' : isForwardReceiveAccessible,
            'namespace'                  : namespace,
            'breadcrumbs'                : self.generateBreadcrumbs(namespace, 'datainputstats'),
            'indexData'                  : indexData, # dict of index stats.
            'winData'                    : showWinInputs,
            'modInputs'                  : modInputs,
            'canView'                    : canView,
            'isDMCDisabled'              : isDMCDisabled,
            'offset'                     : offset,
            'count'                      : count,
            'totalResults'               : totalResults
        })

    def builtin_mod_inputs(self):
        """
        Returns a lookup table for information on builtin modinputs.
        The table is a hash, and is keyed by the modinput names.
        Each entry has the following properties:
        - ignore:
            True if the modinput should not be shown.
        - endpoint:
            The route to the data input. Use this to check xml access.
        - has_manager_xml:
            True if there is manager xml defined for the input, in which case
            a capability check should be performed before showing the input on
            the UI.
        - input_name:
            The name of the corresponding input (as reported by /data/inputs)
        - input_type:
            The input_type for this modinput. Suitable for use as the 'input_type'
            query parameter when creating links to the adddata page.
        - add_as_mod_input:
            True if the `modinput=1` query param should be used when constructing
            the "add new" link to the adddata page.
        """
        builtin_mod_inputs = {}

        def add_mod_input(mod_input_name, **kwargs):
            input_name = kwargs['input_name'] if 'input_name' in kwargs else mod_input_name
            new_input_defaults = {
                'ignore': False,
                'endpoint': ("data/inputs/%s" % input_name),
                'has_manager_xml': True,
                'input_name': input_name,
                'input_type': input_name,
                'add_as_mod_input': False
            }
            for key in kwargs:
                new_input_defaults[key] = kwargs[key]
            builtin_mod_inputs[mod_input_name] = new_input_defaults

        # This input is for internal use only
        add_mod_input('MonitorNoHandle', ignore=True, input_title='Local Windows host monitoring', input_description='Tail a file without useing a file handle.')
        # This input is deprecated. We use the win-event-log-collections
        add_mod_input('WinEventLog', ignore = True, input_title='Local Windows event log monitoring')
        add_mod_input('WinHostMon', input_type = 'hostmon', input_title='Local Windows host monitoring')
        add_mod_input('WinNetMon', input_type = 'netmon', input_title='Local Windows network monitoring')
        add_mod_input('WinPrintMon', input_type = 'printmon', input_title='Local Windows print monitoring')
        # The WinRegMon is deprecated. We use the `registry` input.
        add_mod_input('WinRegMon', ignore = True, input_title='Local Windows registry monitoring')
        # The admon modinput is deprecated. We use the `ad` input.
        add_mod_input('admon', ignore = True, input_title='Active Directory Monitoring')
        # No manager xml for this input. Viewable by all users.
        add_mod_input('powershell', has_manager_xml = False, add_as_mod_input = True, input_title='Powershell v3 Modular Input')
        # The powershell2 is deprecated. We use the `powershell` input above.
        add_mod_input('powershell2', ignore = True, input_title='Powershell v2 Modular Input')
        # This input is deprecated. We use the `win-perfmon` input.
        add_mod_input('perfmon', ignore = True, input_title='Local performance monitoring')

        return builtin_mod_inputs

    """
    Returns the number of inputs returned by the requested dmc endpoint.
    None if a request fails.
    """
    def count_dmc_inputs(self, uri):
        try:
            serverResponse, serverContent = rest.simpleRequest(uri)
            contentJson = json.loads(serverContent)
            cnt = contentJson['paging']['total']
            return cnt
        except Exception as e:
            return None


    @route('/:namespace/:action=adddata_oneshot')
    @expose_page(must_login=True, methods=['POST'])
    def adddata2(self, namespace, action, **kwargs):
        # Release the session lock before proceeding
        cherrypy.session.release_lock()
        saved, err = self.saveUploadFileEntity(kwargs, namespace)
        if err:
            status = 'ERROR'
            msg = 'Error indexing a file'
            logger.debug("Error in adddata_oneshot: error returned by saveUploadFileEntity was: %s" % err)
        else:
            status = 'OK'
            msg = 'Successfully indexed a file'

        return self.render_json({
            'status': status,
            'msg': msg,
            'redirect': ''
            },
            set_mime='text/plain' # jquery form plugin requires json to be return as text/html for its submission-by-iframe trick
        )

    def prepare_breadcrumbs(self, bc, ns):
        if len(bc) > 0:
            crumbs = util.parse_breadcrumbs_string(bc)
        else:
            crumbs = [[_('Manager'), self.make_url(['manager'], translate=False)],
                      [_('Data inputs'), self.make_url(['manager', ns, 'datainputstats'], translate=False)]]

        crumbs.extend([[_('Files & directories'), self.make_url(['manager', ns, 'data/inputs/monitor'], translate=False)],
                       [_('Data preview'), None]])

        return crumbs


    @route('/:namespace/:action=tags')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def tags(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/tags.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'tags'),
        })

    @route('/:namespace/:action=fields')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def fields(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/fields.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'fields'),
        })

    @route('/:namespace/:action=deployment')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def deployment(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/deployment.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'deployment'),
        })

    @route('/:namespace/:action=systemsettings')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def systemsettings(self, namespace, action, operation=None, **kwargs):
        msgid = kwargs.get("msgid")

        showGeneralSettings = self.can_access_manager_xml('server/settings')
        showEmail = self.can_access_manager_xml('admin/alert_actions')
        showLogger = self.can_access_manager_xml('server/logger')
        showDeploymentClient = self.can_access_manager_xml('deployment/client')
        showSearchPrefs = self.can_access_manager_xml('searchprefs')

        if not (showGeneralSettings or
                showEmail or
                showLogger or
                showDeploymentClient or
                showSearchPrefs):
            logger.error('Cannot reach "systemsettings". Insufficient user permissions.')
            raise cherrypy.HTTPError(404)

        return self.render_admin_template('admin/systemsettings.html', {
            'namespace'            : namespace,
            'msgid'                : msgid,
            'breadcrumbs'          : self.generateBreadcrumbs(namespace, 'systemsettings'),
            'showGeneralSettings'  : showGeneralSettings,
            'showEmail'            : showEmail,
            'showLogger'           : showLogger,
            'showDeploymentClient' : showDeploymentClient,
            'showSearchPrefs'      : showSearchPrefs
        })

    @route('/:namespace/:action=advancedsearch')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def advancedsearch(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/advancedsearch.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'advancedsearch'),
        })

    @route('/:namespace/:action=ui')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def ui(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/ui.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'ui'),
        })

    @route('/:namespace/:action=lookups')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def lookups(self, namespace, action, operation=None, **kwargs):
        return self.render_admin_template('admin/lookups.html', {
            'namespace': namespace,
            'breadcrumbs': self.generateBreadcrumbs(namespace, 'lookups'),
        })

    @route('/:namespace/:action=forwardreceive')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def forwardreceive(self, namespace, action, operation=None, **kwargs):

        # for the new link to enable lightweight forwarder... first check that it exists
        lwfExists = False
        try:
            lwf = en.getEntity("apps/local", "SplunkLightForwarder")
            lwfExists = True
        except splunk.ResourceNotFound as e:
            logger.warn("splunkLightForwarder was not found")

        msgid = kwargs.get("msgid")

        can_edit_tcp_defaults = self.can_access_manager_xml('data/outputs/tcp/default')
        can_edit_tcp_server = self.can_access_manager_xml('data/outputs/tcp/server')
        can_edit_tcp_inputs = self.can_access_manager_xml('data/inputs/tcp/cooked')

        if util.isLite():
            if not can_edit_tcp_inputs:
                logger.error('Cannot reach "forwardreceive". Insufficient user permissions.')
                raise cherrypy.HTTPError(404)
        else:
            if not (can_edit_tcp_defaults or
                    can_edit_tcp_server or
                    can_edit_tcp_inputs):
                logger.error('Cannot reach "forwardreceive". Insufficient user permissions.')
                raise cherrypy.HTTPError(404)

        return self.render_admin_template('admin/forwardreceive.html', {
            'lwfExists'   : lwfExists,
            'breadcrumbs' : self.generateBreadcrumbs(namespace, 'forwardreceive'),
            'namespace'   : namespace,
            'msgid'       : msgid,
            'can_edit_tcp_defaults' : can_edit_tcp_defaults,
            'can_edit_tcp_server'   : can_edit_tcp_server,
            'can_edit_tcp_inputs'   : can_edit_tcp_inputs
        })

    @route('/:namespace/:action=distsearch')
    @expose_page(must_login=True, handle_api=True, methods=['GET'])
    def distsearch(self, namespace, action, operation=None, **kwargs):
        from splunk.models.clustering import ClusterConfig

        if not self.can_access_manager_xml('distsearch'):
            logger.error('Cannot reach "distsearch". Insufficient user permissions.')
            raise cherrypy.HTTPError(404)

        statusCode = 0
        nagwareMessaging = {}
        msgid = kwargs.get("msgid")
        try:
            distSearchConfig = en.getEntity("search/distributed/config", "distributedSearch")
        except splunk.RESTException as e:
            logger.warn("Splunk.RESTException: %s", str(e))
            distSearchConfig = None
            statusCode = e.statusCode
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error',
                        '%s' % e.get_message_text())

        breadcrumbs = self.generateBreadcrumbs(namespace, 'distsearch')

        if statusCode == 402:

            logger.info("Distributed search is an Enterprise license-level " +
                    "feature and is currently not available on this instance.")

            return self.render_admin_template('admin/402.html', {
                'feature'             : _('Distributed Search')
            })

        if distSearchConfig:
            distSearchDisabled = normalizeBoolean(distSearchConfig["disabled"])
        else:
            distSearchDisabled = None


        # Determine whether clustering is enabled
        try:
            clusterCfg = ClusterConfig.all()[0]
        except IndexError:
            clusterCfg = None
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', 'The splunkd daemon cannot be reached by splunkweb. Check that there are no blocked network ports or that splunkd is still running.')

        if clusterCfg:
            isClusteringEnabled =  False if clusterCfg.mode == "disabled" else True
        else:
            isClusteringEnabled = False


        logger.debug("\n\n\nisClusteringEnabled: %s \n\n\n" % isClusteringEnabled)


        return self.render_admin_template('admin/distsearch.html', {
            'breadcrumbs'     : breadcrumbs,
            'distSearchDisabled'       : distSearchDisabled,
            'isClusteringEnabled' : isClusteringEnabled,
            'namespace'       : namespace,
            'msgid'           : msgid
        })


    @route('/:namespace/:action=authoverview')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def authoverview(self, namespace, action, operation=None, **kwargs):
        statusCode = 0
        nagwareMessaging = {}
        authmode = None
        msgid = None

        if not self.can_access_manager_xml('authoverview'):
            logger.error('Cannot reach "authoverview". Insufficient user permissions.')
            raise cherrypy.HTTPError(404)

        try:
            active_authmodule = en.getEntity("authentication/providers/services", "active_authmodule")
            authmode = active_authmodule["active_authmodule"]
            logger.info("active authmode: %s." % authmode)
        except splunk.ResourceNotFound as e:
            raise cherrypy.HTTPError(404, _('Splunk cannot find "%s/%s".')
                % (namespace, action))
        except splunk.AuthorizationFailed as e:
            error = "Fail: %s." % e.msg
            msgid = MsgPoolMgr.get_poolmgr_instance()[UI_MSG_POOL].push('error', error)
            return self.redirect_to_url("/manager/%s" % namespace, _qs={
                "msgid" : msgid
            } )
        except splunk.RESTException as e:
            logger.debug("RESTException.")
            statusCode = e.statusCode
            logger.info("e: %s" % e)

        try:
            duoConfig = en.getEntity("admin/Duo-MFA", "duo-mfa")
            duoConfig['enabled'] = True
        except Exception as e:
            # if not found then initialize an empty config.
            duoConfig = {
                'enabled': False,
                'integrationKey': '',
                'apiHostname': '',
                'appSecretKey': '',
                'failOpen': False,
                'secretKey': '',
                'timeout': 30
            }

        try:
            rsaConfig = en.getEntity("admin/Rsa-MFA", "rsa-mfa", namespace=namespace)
            rsaConfig['enabled'] = True
        except Exception as e:
            # if not found then initialize an empty config.
            rsaConfig = {
                'enabled': False,
                'authManagerUrl': '',
                'accessKey': '',
                'caCertBundlePayload': '',
                'failOpen': False,
                'messageOnError': '',
                'clientId': '',
                'timeout': 15
            }

        breadcrumbs = self.generateBreadcrumbs(namespace, 'authoverview')

        if statusCode == 402:

            return self.render_admin_template('admin/402.html', {
                'feature'             : _("Authentication method")
            })


        return self.render_admin_template('admin/authoverview.html', {
            'namespace'             : namespace,
            'breadcrumbs'           : breadcrumbs,
            'authmode'              : authmode, # currently active splunk auth provider.
            'duoConfig': duoConfig,
            'rsaConfig': rsaConfig
        })

    @expose_page(must_login=True, handle_api=True, methods=['POST'])
    def uploadbgimg(self, **kargs):
        if not self.can_access_manager_xml_file('login_page_settings'):
            logger.error('admin.py:upload:handler no sufficient privileges to access this page.')
            self.deny_access()

        image = kargs.get('image', None)
        filename = kargs.get('filename', None)
        extension = filename.lower().split('.')[-1]
        if extension not in AUTHORIZED_FILE_EXTENSIONS:
            logger.error('admin.py:upload:handler format of the file not supported.')
            self.deny_access()

        if image is not None :
            # checking for file signatures: JPG-JPEG \xFF\xD8\xFF and PNG \x89\x50\x4E\x47\x0D\x0A\x1A\x0A.
            # https://en.wikipedia.org/wiki/List_of_file_signatures
            if not (image.value.startswith('\xFF\xD8\xFF') or image.value.startswith('\x89\x50\x4E\x47\x0D\x0A\x1A\x0A')):
                logger.error('admin.py:upload:handler signature of the file seems wrong or format not supported.')
                self.deny_access()
            try:
                tempPath = util.make_splunkhome_path(['etc', 'apps', 'search', 'appserver', 'static', 'logincustombg'])
                if not os.path.exists(tempPath):
                    os.makedirs(tempPath)

                if filename.find('/')>-1 or filename.find('\\')>-1 or filename.startswith('.'):
                    return 'Filename cannot contain / or \\ character or start with . filename="%s"' % filename

                newPath = os.path.join(tempPath, filename)
                with open(newPath, 'wb') as newFile:
                    copyfileobj(image.file, newFile)

                logger.info('admin.py:upload:handler upload of the new background image successful.')
                return "Successfully stored %s" % filename
            except Exception as e:
                logger.error('admin.py:upload:handler error uploading the new background image.')
                return 'Failed to upload the file %s. Exception %s' % (filename, str(e))
        else:
            logger.error('admin.py:upload:handler cannot upload the new background image: no image provided.')
            raise cherrypy.HTTPError(400, 'No image provided.')

    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def control(self, operation=None):
        if cherrypy.request.method == 'POST':
            if operation == 'restart_server':
                uri = '/services/server/control'
                postargs = {'_action':'restart'}
                try:
                    serverResponse, serverContent = rest.simpleRequest(uri, postargs=postargs)

                    # Pulling the X-Forwarded-Host header here allows for splunk to be restarted
                    # from the UI without misdirecting the user to an incorrect host, port or protocol
                    # when splunkweb is behind a proxy.
                    x_host = cherrypy.request.headers.get('X-Forwarded-Host')
                    if x_host:
                        port = len(x_host.split(':')) > 1 and x_host.split(':')[-1] or 80
                        ssl = 'window'
                    else:
                        try:
                            # Fetch the Port and SSL enabled status so the JS knows where to poll for status
                            ent = en.getEntity('server/settings', 'settings')
                            port = int(ent['httpport'])
                            ssl = normalizeBoolean(ent['enableSplunkWebSSL'])
                        except Exception as e:
                            # Use the Host header if the fetch fails
                            host = cherrypy.request.headers.get('Host') or ''
                            port = len(host.split(':')) > 1 and host.split(':')[-1] or 80
                            ssl = 'window'

                    response = {
                        'status': 'OK',
                        'start_time': round(cherrypy.config['start_time']),
                        'port': port,
                        'ssl': ssl
                    }

                except splunk.AuthenticationFailed:
                    response = { 'status': 'AUTH' }
                except splunk.AuthorizationFailed:
                    response = { 'status': 'PERMS' }
                return self.render_json(response)
            elif operation == 'resync_auth':
                uri = '/services/authentication/providers/services/_reload'
                postargs = {'name':'restart'}
                try:
                    serverResponse, serverContent = rest.simpleRequest(uri, postargs=postargs)
                except splunk.AuthenticationFailed:
                    return 'AUTH'
                except splunk.AuthorizationFailed:
                    return 'PERMS'
                if serverResponse.status in (201, 200):
                    return 'OK'
                return serverResponse
            else:
                return _('Invalid operation')
        return _('Invalid operation %s') % cherrypy.request.method

    @route('/:namespace/:action=control')
    @expose_page(must_login=True, handle_api=False, methods=['GET'])
    def controlpage(self, namespace, action, operation=None, **kwargs):

        if not self.can_access_manager_xml('control'):
            logger.error('Cannot reach "control". Insufficient user permissions.')
            raise cherrypy.HTTPError(404)

        msgid = kwargs.get('msgid', None)
        auto_restart = normalizeBoolean(kwargs.get('auto_restart', False))
        serverControls = en.getEntities("server/control")
        restartLink = any(filter((lambda x: x[0] == 'restart'), serverControls.links))

        # if splunkweb is disabled, restarting via web will never come back
        canRestartSplunkweb = True
        poison_apps = splunk.models.app.App.all().filter(name='SplunkLightForwarder', is_disabled=False)
        if len(poison_apps):
            canRestartSplunkweb = False
        elif not util.isCloud(): #web must be ON for cloud and /server/settings are not accessible to sc_admins
            server_settings = en.getEntity('server/settings', 'settings')
            if not normalizeBoolean(server_settings['startwebserver']):
                canRestartSplunkweb = False

        try:
            rest.simpleRequest('/messages/restart_required')
            restart_required = True
        except splunk.ResourceNotFound:
            restart_required = False

        if restartLink:
            displayRestartButton = True
            displayClearRestartButton = restart_required
        else:
            displayRestartButton = False
            displayClearRestartButton = False

        # JIRA:STEWIE-705: clear restart message link produces 404
        if util.isLite():
            displayClearRestartButton = False

        return self.render_admin_template('admin/control.html', {
            'namespace'                 : namespace,
            'canRestartSplunkweb'       : canRestartSplunkweb,
            'displayRestartButton'      : displayRestartButton,
            'displayClearRestartButton' : displayClearRestartButton,
            'msgid'                     : msgid,
            'auto_restart'              : auto_restart,
            'return_to'                 : kwargs.get('return_to', ''),
            'breadcrumbs'               : self.generateBreadcrumbs(namespace, 'control'),
            'isCloud'                   : util.isCloud()
        })

    @route('/:namespace/:endpoint_base=widget-hiding')
    @expose_page(must_login=True, methods=['GET'])
    def listWidgetHidingEntities(self, namespace, endpoint_base, **kwargs):
        """
        Endpoint that demos the principal of widget actions triggering the display/hiding
        of other widgets
        """
        error = None
        msgid = kwargs.get('msgid', None)
        return self.render_admin_template('admin/index.html', {
            'namespace'     : namespace,
            'breadcrumbs'       : self.generateBreadcrumbs(namespace, 'widget-hiding'),
            'entities'      : ['Demo'],
            'entity'        : None,
            'uiHelper'      : {},
            'kwargs'      : {},
            'endpoint_path' : endpoint_base,
            'endpoint_base' : endpoint_base,
            'error'         : error,
            'msgid'         : msgid
        })

    @route('/:namespace/:endpoint_base=widget-hiding/:widget_name=Demo', methods=['GET', 'POST'])
    @expose_page(must_login=True)
    def listWidgetHidingProperties(self, namespace, endpoint_base, widget_name, **kwargs):
        error = None
        outputFormat = kwargs.get('outputFormat', 'HTML')
        form_errors = {}
        form_defaults = {}
        uiHelper = self.fetchUIHelpers(search=endpoint_base, namespace=namespace)

        uiHelper_elements = {}
        self.flattenElements(uiHelper['elements'], uiHelper_elements)

        return self.render_admin_template('admin/index.html', {
            'namespace'         : namespace,
            'endpoint_path'     : endpoint_base,
            'endpoint_base'     : endpoint_base, # url base from admin ie: saved-searches
            'uiHelper'          : uiHelper, # dictionary of values for markup help
            'uiHelper_elements' : uiHelper_elements, # dictionary of elements organized by element name
            'entity_name'       : widget_name,
            'form_errors'       : {}, # dict of form errors
            'form_defaults'     : {'dummy':'dummy'}, # dict of entity values.
            'error'             : error
        })

    @route('/:namespace/:endpoint_base=view-widgets')
    @expose_page(must_login=True, methods=['GET'])
    def listViewWidgetsEntities(self, namespace, endpoint_base, **kwargs):
        """
        Create a virtual endpoint called view-widgets that lists the unqiue
        widgets currently defined in uiHelpers
        """
        widgets = self.loadUniqueWidgets() # find unique widgets defined in uiHelpers
        error = None
        msgid = kwargs.get('msgid', None)
        return self.render_admin_template('admin/index.html', {
            'namespace'         : namespace,
            'entities'      : ['All Widgets'] + [name for name in widgets if name!='hidden'],
            'endpoint_path' : endpoint_base,
            'endpoint_base' : endpoint_base,
            'error'         : error,
            'msgid'         : msgid
        })

    @route('/:namespace/:endpoint_base=view-widgets/:widget_name')
    @expose_page(must_login=True, methods=['GET', 'POST'])
    def listWidgetProperties(self, namespace, endpoint_base, widget_name, **kwargs):
        """
        display examples of one or more widget types by creating a fake uiHelper
        The examples are taken from the live uiHelper config
        This should be good enough for our current widgets
        """
        widgets = self.loadUniqueWidgets()
        error = None
        outputFormat = kwargs.get('outputFormat', 'HTML')
        form_errors = {}
        form_defaults = {}

        widget_names = widgets.keys() if widget_name=='All Widgets' else [widget_name]
        uiHelper = {'header' : widget_name, 'elements' : []} # fake up a uiHelpers for this virtual endpoint
        for name in widget_names:
            if name not in widgets:
                error = _('The widget name is invalid.')
            else:
                element = widgets[name].copy()
                if 'label' in element:
                    element['label'] = '(%s) %s' % (name, element['label'])
                uiHelper['elements'].append(element)

        uiHelper_elements = {}
        self.flattenElements(uiHelper['elements'], uiHelper_elements)

        if outputFormat == "XML":
            # format of XML returns the raw feed

            transformer = kwargs.get('transformer', None)
            if not transformer == None:
                # insert the transformer xslt document into the xml.
                # validate the xsl is xsl file and path is available.
                pass
            pass
        else:
            return self.render_admin_template('admin/index.html', {
                'namespace'         : namespace,
                'endpoint_path'     : endpoint_base,
                'endpoint_base'     : endpoint_base, # url base from admin ie: saved-searches
                'uiHelper'          : uiHelper, # dictionary of values for markup help
                'uiHelper_elements' : uiHelper_elements, # dictionary of elements organized by element name
                'entity_name'       : widget_name,
                'form_errors'       : {}, # dict of form errors
                'form_defaults'     : {'dummy':'dummy'}, # dict of entity values.
                'error'             : error
            })

    def _getRemoteEntries(self, **kwargs):
        '''
        Returns entities returned by the /apps/remote endpoint

        Arguments:
            category        An encoded category name to filter on
            type            An encoded name to filter on
            agent           Splunk agent string - Will platform-filter results based on this string
            allplatforms    If set then the platform restriction caused by setting the agent parameter is removed
            q               String to match anywhere in an application title or short description
            name            Name of an application (exact match) - May be repeated to match multiple applications
            splunk_version  If set then only return apps that claim compatiblity with the specified Splunk version - eg. "3.1.2" - Must only contain digits and periods.
            sort_by         Result sort order - May be one of 'name', 'downloads' or 'updateTime' (default)
            sort_dir        Set sort order - May be one of 'desc' or 'asc' - Default is desc for updateTime and asc for name
            offset          An integer offset to start from (default 0)
            count           Integer maximum number of results to return
        '''
        url = 'apps/remote/entries'

        if not 'splunk_version' in kwargs:
            kwargs['splunk_version'] = '4.1.99'
        if 'q' in kwargs and kwargs['q'] == '':
            del kwargs['q']

        entities = en.getEntities(url, **kwargs)
                
        return (list(entities.values()), int(entities.totalResults))

    def _prepareEntries(self, entries):
        url = 'apps/local'
        en_local = en.getEntities(url, count=-1)

        sbIdsOfLocalApps = {}
        for app in en_local:
            if 'details' in en_local[app]:
                sbId = en_local[app]['details'][en_local[app]['details'].rfind('/')+1:]
                sbIdsOfLocalApps[sbId] = app

        for e in entries:
            # Bring date to simple format
            e['dateAddonUpdated'] = parseISO(e['dateAddonUpdated']).strftime('%m/%d/%y')

            # Truncate summary to the first sentence
            m = re.match("(^.+?[.!?]+)(?=\s+|$)", e.summary, re.DOTALL)
            if m:
                e.summary = m.groups()[0]
                e.summary += '..'

            # Check if installed and has update
            if e['appID'] in sbIdsOfLocalApps:
                e['installed'] = True
                localAppId = sbIdsOfLocalApps[e['appID']]
                updateLink = any(filter((lambda x: x[0] == 'update'), en_local[localAppId].links))
                if updateLink:
                    e['update_available'] = True
                    e['implicit_id_required'] = normalizeBoolean(en_local[localAppId].get('update.implicit_id_required', None))

    @route('/:namespace/:apps=apps/:remote=remote')
    @expose_page(must_login=True, handle_api=True, methods=['GET', 'POST'])
    def splunkbase_browser(self, namespace, apps, remote, msgid=None, **kwargs):
        ## handle any possible legacy links to old appbrowser and redirect to new version
        raise self.redirect_to_url(['manager', namespace, 'appsremote'])

def unit_test():
    adm = AdminController()

if __name__ == '__main__':
    unit_test()
