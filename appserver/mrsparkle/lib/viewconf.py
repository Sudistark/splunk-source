#
# Splunk UI view configuration serialization services
#
# The view configuration files are XML descriptions that define both the functional
# and cosmetic layout of modules inside the view.  The view files are stored on a
# per-app basis inside the /etc/apps/* directories
#


import defusedxml.lxml as safe_lxml
import logging
import lxml.etree as et
import os

import cherrypy
import splunk
import splunk.util
import splunk.models.view_escaping.fromdash as fromdash
import splunk.appserver.mrsparkle.lib.util as util

logger = logging.getLogger('splunk.appserver.mrsparkle.lib.viewconf')

# define list of attributes that can appear on the top level <view> node
ROOT_VIEW_ATTRIBUTES = [
    'autoCancelInterval',
    'decomposeIntentions',
    'displayView',
    'isPersistable',
    'isSticky',
    'isVisible',
    'nativeObjectMode',
    'objectMode',
    'onunloadCancelJobs',
    'refresh',
    'stylesheet',
    'target',
    'template',
    'type'
]


# /////////////////////////////////////////////////////////////////////////////
# Object -> XML serialization
# /////////////////////////////////////////////////////////////////////////////

def dumps(obj):
    '''
    Serializes a view object configuration into Splunk 4.0 XML view
    configuration format string.
    '''

    root = et.Element('view')

    #if 'isVisible' in obj:
    #    if obj['isVisible'] == False:
    #        root.attrib['isVisible'] = 'false'

    for k in sorted(ROOT_VIEW_ATTRIBUTES):
        if k in obj:
            if obj[k] in (True, False):
                root.set(k, splunk.util.unicode(obj[k]).lower())
            elif obj[k] in (None, ''):
                continue
            else:
                root.set(k, splunk.util.unicode(obj[k]))

    if 'label' in obj:
        l = et.Element('label')
        l.text = obj['label']
        root.append(l)

    for module in obj['modules']:
        root.append(_moduleToXmlNode(module))

    return et.tostring(root, pretty_print=True, encoding='utf-8', xml_declaration=False)


def _moduleToXmlNode(module):
    '''
    Converts a module dict into XML nodes
    '''

    m = et.Element('module')
    m.set('name', module['className'])

    # get the top-level attributes
    for k in ['layoutPanel']:
        if module.get(k) != None:
            m.set(k, splunk.util.unicode(module[k]))

    if 'params' in module:
        # get the injected attributes that belong at top-level
        for k in ['group', 'autoRun']:
            if module['params'].get(k) != None:
                m.set(k, splunk.util.unicode(module['params'][k]))
                del module['params'][k]

        _paramsToXmlNode(module['params'], m)

    if 'children' in module:
        for child in module['children']:
            m.append(_moduleToXmlNode(child))

    return m


def _paramsToXmlNode(data, root):
    '''
    Converts a module's params dict into XML nodes
    '''

    if isinstance(data, dict):
        for k in data:
            n = et.Element('param')
            n.set('name', k)
            if isinstance(data[k], list) or isinstance(data[k], dict):
                _paramsToXmlNode(data[k], n)
            elif data[k] in (None, ''):
                # SPL-48485, SPL-55051: write out an empty node for selected params
                if k in ['series', 'value']:
                    n.text = ''
                # otherwise don't write out empty defaults
                else:
                    continue
            else:
                n.text = splunk.util.unicode(data[k])
            root.append(n)

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, list) or isinstance(item, dict):
                n = et.Element('list')
                _paramsToXmlNode(item, n)
            else:
                n = et.Element('item')
                n.text = splunk.util.unicode(item)
            root.append(n)

    else:
        raise Exception('Unable to convert data type: %s' % type(data))

    return root



# /////////////////////////////////////////////////////////////////////////////
# XML -> object deserialization
# /////////////////////////////////////////////////////////////////////////////


def load(filePath):
    '''
    Parses a Splunk 4.0 view XML configuration file into native objects
    '''

    viewName = filePath.split(os.sep)[-1]
    viewName = viewName.split('.')[0]
    root = safe_lxml.parse(filePath)
    return _viewXmlToObject(root, viewName)

def get_dashboard_template_from_version(version):
    if version == '2.0':
        return util.getDashboardV2TemplateUri()
    if version == '1.1':
        return util.getDashboardV1p1TemplateUri()
    if version == '1.0':
        return util.getDashboardV1TemplateUri()
    # Fallback to 1.1
    return util.getDashboardV1p1TemplateUri()

def get_temporary_xml_load_version():
    if 'xmlv' not in cherrypy.request.params:
        return None
    return cherrypy.request.params['xmlv']

def get_simplexml_dashboard_version(root_version, url_xmlv):
    default_version = '1.1'
    valid_xml_versions = { '1.0', '1.1' }
    is_valid_url_xmlv = url_xmlv is not None and url_xmlv in valid_xml_versions
    load_updated_temp_view = root_version == '1.0' and is_valid_url_xmlv and url_xmlv != '1.0'
    def get_candidate_version():
        # 1. Explicitly declared xml source version
        # * As long we shouldn't load an up-to-date temp view
        if root_version is not None and root_version.startswith('1.') and load_updated_temp_view == False:
            return root_version
        # 2. Load temporary dashboard version (for allowed versions only)
        if is_valid_url_xmlv:
            return url_xmlv
        # 3. Load version 1.1 by default
        return default_version
    candidate_version = get_candidate_version()
    # If candidate load version is 1.0 but template does not exist, load 1.1
    v1_template_path = 'share/splunk/search_mrsparkle/templates%s' % util.getDashboardV1TemplateUri()
    v1_template_exists = util.splunk_home_file_exists(v1_template_path)
    v1_exists = cherrypy.config.get('enable_jQuery2', True) and util.splunk_home_file_exists(v1_template_path)
    return default_version if (candidate_version == '1.0' and not v1_exists) else candidate_version

def loads(xmlString, viewName, flashOk=True, viewDigest=None, sourceApp=None):
    '''
    Parses a Splunk 4.0 view XML configuration string into native objects
    '''

    parser = et.XMLParser(remove_blank_text=True, remove_comments=False, remove_pis=True)
    # Per SPL-32256, our XML parser only wants ascii and will handle dealing with
    # utf-8 encoding itself.
    xmlString = xmlString.encode('ascii', 'xmlcharrefreplace')

    root = safe_lxml.fromstring(xmlString, parser=parser)

    # the version 2 dashboards can only be rendered by a separate app as of Pinkie Pie release
    if root.tag == 'dashboard' and root.get('version') == '2':
        # this hard-coded string needs to match the one in controllers/view.py render() function
        # for some reason 'modules' key is needed for this output
        return {'template': util.getDashboardV2TemplateUri(), 'modules': {}}
    elif root.tag in ['dashboard', 'form']:
        src_version = root.get('version')
        url_xmlv = get_temporary_xml_load_version()
        version = get_simplexml_dashboard_version(src_version, url_xmlv)
        v1_template = get_dashboard_template_from_version(version)
        return {'simplexml': xmlString, 'template': v1_template, 'modules': {}, 'isSimpleXML': True}
    else:
        return _viewXmlToObject(root, viewName)


def loadLegacy(xmlString, viewName, viewDigest=None, sourceApp=None):
    parser = et.XMLParser(remove_blank_text=True, remove_comments=False, remove_pis=True, resolve_entities=False)
    xmlString = xmlString.encode('ascii', 'xmlcharrefreplace')
    root = safe_lxml.fromstring(xmlString, parser=parser)
    model = fromdash.createDashboardFromXml(root, viewName=viewName, digest=viewDigest, sourceApp=sourceApp)
    template = '/dashboards/dashboard.html'
    output = {'dashboard': model, 'template': template, 'modules': {}, 'isSimpleXML': True}
    # add standard props
    for k in model.standardAttributeMap:
        output[k] = getattr(model, k)
    return output


def _viewXmlToObject(lxmlNode, name):

    output = {
        'label': lxmlNode.findtext('label', name),
        'objectMode': lxmlNode.get('objectMode', 'viewconf'),
        'nativeObjectMode': 'viewconf',
        'modules': []
    }

    for node in lxmlNode:
       if node.tag == 'module':
          if not node.get('name'):
            logger.warn('Unable to process view module declaration with missing name attribute; view=%s' % name)
            continue
          output['modules'].append(_moduleNodeToObject(node))
       elif node.tag == 'simpleChain':
          for m in node.findall('module'):
             if not m.get('name'):
                logger.warn('Unable to process view module declaration with missing name attribute; view=%s' % name)
                continue
             output['modules'].append(_moduleNodeToObject(m))

    # by default, views are visible; if XML explicitly declares visibility
    # make it so
    v = lxmlNode.get('isVisible', True)
    output['isVisible'] = splunk.util.normalizeBoolean(v)

    # by default, don't decompose intentions
    v = lxmlNode.get('decomposeIntentions', False)
    output['decomposeIntentions'] = splunk.util.normalizeBoolean(v)


    # by default, saved searches are loaded back into the view they were created in.
    # for some views, like report creator, this may not be the desired behaviour.
    # display_view lets the view creator specify which view is used to load a search.
    # also, read in how often the view shall refresh, in seconds
    for attrib in ['displayView', 'refresh', 'stylesheet']:
        a = lxmlNode.get(attrib, None)
        if a: output[attrib] = a

    # by default, searches are canceled on the onunload of the page; if XML explicitly declares onunloadCancelJobs
    v = lxmlNode.get('onunloadCancelJobs', True)
    output['onunloadCancelJobs'] = splunk.util.normalizeBoolean(v)

    # by default searches are cancelled if they run unattended for more than 90 seconds.
    autoCancelInterval = lxmlNode.get('autoCancelInterval', 90)
    try:
        autoCancelInterval = int(autoCancelInterval)
    except:
        logger.warn('cannot cast autoCancelInterval to integer, resetting to default')
        autoCancelInterval = 90
    output['autoCancelInterval'] = autoCancelInterval

    # bypass stickiness features, set to False to disable.
    v = lxmlNode.get('isSticky', True)
    output['isSticky'] = splunk.util.normalizeBoolean(v)

    # bypass stickiness features, set to False to disable.
    v = lxmlNode.get('isPersistable', True)
    output['isPersistable'] = splunk.util.normalizeBoolean(v)

    # default template is 'search.html'
    output['template'] = lxmlNode.get('template', 'search.html')

    # default to enable module system
    output['type'] = lxmlNode.get('type', 'module')

    # default to enable module system
    output['target'] = lxmlNode.get('target', None)

    return output


def _moduleNodeToObject(lxmlNode):

    output = {}

    output['className'] = lxmlNode.get('name', '').strip()

    x = lxmlNode.get('layoutPanel')
    if x:
        output['layoutPanel'] = x.strip()

    keyedParamMap = {}

    params = lxmlNode.findall('param')
    if params:
        output['params'] = {}
        for node in params:
            output['params'][node.get('name')] = _paramNodeToObject(node, keyedParamMap)

    if len(keyedParamMap) > 0:
        output['keyedParamMap'] = keyedParamMap

    autoRun = lxmlNode.get('autoRun')
    if autoRun:
        output.setdefault('params', {})
        output['params']['autoRun'] = autoRun

    group =  lxmlNode.get('group')
    if group:
        output.setdefault('params', {})
        output['params']['group'] = group
        output['params']['groupLabel'] = _(group)

    altTitle =  lxmlNode.get('altTitle')
    if altTitle:
        output.setdefault('params', {})
        output['params']['altTitle'] = _(altTitle)

    nodes = lxmlNode.findall('module')
    if nodes:
        output['children'] = []
        for node in nodes:
            output['children'].append(_moduleNodeToObject(node))

    return output


def _paramNodeToObject(lxmlNode, keyedParamMap):
    '''
    Recursively deserializes a <param> node into a native data structure of
    dict and list objects.  The keyedParamMap is a reference to a dictionary
    of keyed parameters, used in aliasing specific parameters within the view
    XML.
    '''

    # current node is leaf node
    if lxmlNode.text:

        # check if param has been assigned a key
        if lxmlNode.get('pkey') != None:
            if lxmlNode.get('name') == None:
                logger.warn('Cannot assign persistence key to <param> node with a "name" attribute')
            elif lxmlNode.get('name') in keyedParamMap:
                logger.warn('Persistence key "%s" already exists in current module node' % lxmlNode.get('name'))
            else:
                keyedParamMap[lxmlNode.get('name')] = lxmlNode.get('pkey')

        # return the text content
        return lxmlNode.text.strip()


    # determine the output type based on the first node to match either <list> or <param>
    # SPL-54462: can't just look at the first sub-node because it could be a comment
    output = None
    if len(lxmlNode):
        if lxmlNode.find('list') is not None:
            output = []
            for subnode in lxmlNode:
                # SPL-54462: since we are iterating over all child nodes blindly, make sure to exclude comments
                if subnode.tag is not et.Comment:
                    output.append(_paramNodeToObject(subnode, keyedParamMap))

        elif lxmlNode.find('param') is not None:
            output = {}
            for subnode in lxmlNode:
                # SPL-54462: since we are iterating over all child nodes blindly, make sure to exclude comments
                if subnode.tag is not et.Comment:
                    output[subnode.get('name')] = _paramNodeToObject(subnode, keyedParamMap)

    return output

# Tests
import unittest

class LoadsSuite(unittest.TestCase):
    def test_v1_root_is_dashboard(self):
        viewConfig = loads('<dashboard></dashboard>', 'fake_dashboard_id')
        self.assertEqual(viewConfig, { 'template': '/pages/dashboard_1.1.html', 'simplexml': b'<dashboard></dashboard>', 'isSimpleXML': True, 'modules': {} })

    def test_v1_root_is_form(self):
        viewConfig = loads('<form></form>', 'fake_dashboard_id')
        self.assertEqual(viewConfig, { 'template': '/pages/dashboard_1.1.html', 'simplexml': b'<form></form>', 'isSimpleXML': True, 'modules': {} })

    def test_v1p1_root_is_dashboard(self):
        viewConfig = loads('<dashboard version="1.1"></dashboard>', 'fake_dashboard_id')
        self.assertEqual(viewConfig, { 'template': '/pages/dashboard_1.1.html', 'simplexml': b'<dashboard version="1.1"></dashboard>', 'isSimpleXML': True, 'modules': {} })

    def test_v1p1_root_is_form(self):
        viewConfig = loads('<form version="1.1"></form>', 'fake_dashboard_id')
        self.assertEqual(viewConfig, { 'template': '/pages/dashboard_1.1.html', 'simplexml': b'<form version="1.1"></form>', 'isSimpleXML': True, 'modules': {} })

class GetSimpleXMLVersionSuite(unittest.TestCase):

    # +------+--------------+----------+--------------------+--------+
    # | case | root_version | url_xmlv | v1_template_exists | result |
    # +------+--------------+----------+--------------------+--------+
    # | 1    | None         | None     | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 2    | None         | 1.0      | True               | 1.0    |
    # +------+--------------+----------+--------------------+--------+
    # | 2.1  | None         | 1.0      | False              | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 3    | None         | 1.1      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 4    | None         | 2.0      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 5    | 1.0          | None     | True               | 1.0    |
    # +------+--------------+----------+--------------------+--------+
    # | 5.1  | 1.0          | None     | False              | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 6    | 1.0          | 1.0      | True               | 1.0    |
    # +------+--------------+----------+--------------------+--------+
    # | 6.1  | 1.0          | 1.0      | False              | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 7    | 1.0          | 1.1      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 8    | 1.0          | 2.0      | True               | 1.0    |
    # +------+--------------+----------+--------------------+--------+
    # | 8.1  | 1.0          | 2.0      | False              | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 9    | 1.1          | None     | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 10   | 1.1          | 1.0      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 11   | 1.1          | 1.1      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 12   | 1.1          | 2.0      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 13   | 3.0          | None     | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 14   | 3.0          | 1.0      | True               | 1.0    |
    # +------+--------------+----------+--------------------+--------+
    # | 14.1 | 3.0          | 1.0      | False              | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 15   | 3.0          | 1.1      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+
    # | 16   | 3.0          | 2.0      | N/A                | 1.1    |
    # +------+--------------+----------+--------------------+--------+

    def setUp(self):
        splunk_home = os.environ.get('SPLUNK_HOME')
        self.v1_template_path = os.path.join(splunk_home, 'share/splunk/search_mrsparkle/templates/pages/dashboard.html')
        self.hidden_v1_template_path = os.path.join(splunk_home, 'share/splunk/search_mrsparkle/templates/pages/dashboard.html.hidden')

    def tearDown(self):
        if os.path.exists(self.hidden_v1_template_path):
            os.rename(self.hidden_v1_template_path, self.v1_template_path)

    def test_case_1(self):
        version = get_simplexml_dashboard_version(None, None)
        self.assertEqual(version, '1.1')

    def test_case_2(self):
        version = get_simplexml_dashboard_version(None, '1.0')
        self.assertEqual(version, '1.0')

    def test_case_2p1(self):
        os.rename(self.v1_template_path, self.hidden_v1_template_path)
        version = get_simplexml_dashboard_version(None, '1.0')
        self.assertEqual(version, '1.1')

    def test_case_3(self):
        version = get_simplexml_dashboard_version(None, '1.1')
        self.assertEqual(version, '1.1')

    def test_case_4(self):
        version = get_simplexml_dashboard_version(None, '2.0')
        self.assertEqual(version, '1.1')

    def test_case_5(self):
        version = get_simplexml_dashboard_version('1.0', None)
        self.assertEqual(version, '1.0')

    def test_case_5p1(self):
        os.rename(self.v1_template_path, self.hidden_v1_template_path)
        version = get_simplexml_dashboard_version('1.0', None)
        self.assertEqual(version, '1.1')

    def test_case_6(self):
        version = get_simplexml_dashboard_version('1.0', '1.0')
        self.assertEqual(version, '1.0')

    def test_case_6p1(self):
        os.rename(self.v1_template_path, self.hidden_v1_template_path)
        version = get_simplexml_dashboard_version('1.0', '1.0')
        self.assertEqual(version, '1.1')

    def test_case_7(self):
        version = get_simplexml_dashboard_version('1.0', '1.1')
        self.assertEqual(version, '1.1')

    def test_case_8(self):
        version = get_simplexml_dashboard_version('1.0', '2.0')
        self.assertEqual(version, '1.0')

    def test_case_8p1(self):
        os.rename(self.v1_template_path, self.hidden_v1_template_path)
        version = get_simplexml_dashboard_version('1.0', '2.0')
        self.assertEqual(version, '1.1')

    def test_case_9(self):
        version = get_simplexml_dashboard_version('1.1', None)
        self.assertEqual(version, '1.1')

    def test_case_10(self):
        version = get_simplexml_dashboard_version('1.1', '1.0')
        self.assertEqual(version, '1.1')

    def test_case_11(self):
        version = get_simplexml_dashboard_version('1.1', '1.1')
        self.assertEqual(version, '1.1')

    def test_case_12(self):
        version = get_simplexml_dashboard_version('1.1', '2.0')
        self.assertEqual(version, '1.1')

    def test_case_13(self):
        version = get_simplexml_dashboard_version('3.0', None)
        self.assertEqual(version, '1.1')

    def test_case_14(self):
        version = get_simplexml_dashboard_version('3.0', '1.0')
        self.assertEqual(version, '1.0')

    def test_case_14p1(self):
        os.rename(self.v1_template_path, self.hidden_v1_template_path)
        version = get_simplexml_dashboard_version('3.0', '1.0')
        self.assertEqual(version, '1.1')

    def test_case_15(self):
        version = get_simplexml_dashboard_version('3.0', '1.1')
        self.assertEqual(version, '1.1')

    def test_case_16(self):
        version = get_simplexml_dashboard_version('3.0', '2.0')
        self.assertEqual(version, '1.1')

    def test_case_17(self):
        version = get_simplexml_dashboard_version('1.0', '1.0.0')
        self.assertEqual(version, '1.0')

if __name__ == '__main__':
    # run tests
    loads_suite = unittest.TestLoader().loadTestsFromTestCase(LoadsSuite)
    unittest.TextTestRunner(verbosity=2).run(loads_suite)

    get_simplexml_version_suite = unittest.TestLoader().loadTestsFromTestCase(GetSimpleXMLVersionSuite)
    unittest.TextTestRunner(verbosity=2).run(get_simplexml_version_suite)
