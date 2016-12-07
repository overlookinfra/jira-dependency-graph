#!/usr/bin/env python

from __future__ import print_function

import argparse
import json
import socket
import sys
import time

import requests


# Using REST is pretty simple. The vast majority of this code is about the "other stuff": dealing with
# command line options, formatting graphviz, calling Google Charts, etc. The actual JIRA REST-specific code
# is only about 5 lines.

GOOGLE_CHART_URL = 'http://chart.apis.google.com/chart?'


def log(*args):
    print(*args, file=sys.stderr)


class JiraSearch(object):
    """ This factory will create the actual method used to fetch issues from JIRA. This is really just a closure that saves us having
        to pass a bunch of parameters all over the place all the time. """

    def __init__(self, url, auth):
        self.url = url
        self.auth = auth
        self.fields = ','.join(['key', 'issuetype', 'issuelinks', 'subtasks', 'status', 'summary'])

    def get(self, uri, params={}):
        headers = {'Content-Type' : 'application/json'}
        url = self.url + '/rest/api/latest/' + uri

        if isinstance(self.auth, str):
            return requests.get(url, params=params, cookies={'JSESSIONID': self.auth}, headers=headers)
        else:
            return requests.get(url, params=params, auth=self.auth, headers=headers)

    def get_issue(self, key):
        """ Given an issue key (i.e. JRA-9) return the JSON representation of it. This is the only place where we deal
            with JIRA's REST API. """
        log('Fetching ' + key)
        # we need to expand subtasks and links since that's what we care about here.
        response = self.get('/issue/%s' % key, params={'fields': self.fields})
        response.raise_for_status()
        return response.json()

    def query(self, query):
        log('Querying ' + query)
        # TODO comment
        response = self.get('/search', params={'jql': query, 'fields': self.fields})
        content = response.json()
        return content['issues']


def build_graph_data(start_issue_key, jira, excludes, show_directions, directions):
    """ Given a starting image key and the issue-fetching function build up the GraphViz data representing relationships
        between issues. This will consider both subtasks and issue links.
    """
    def get_key(issue):
        return issue['key']

    def process_link(issue_key, link):
        if link.has_key('outwardIssue'):
            direction = 'outward'
        elif link.has_key('inwardIssue'):
            direction = 'inward'
        else:
            return

        if direction not in directions:
            return

        linked_issue = link[direction + 'Issue']
        linked_issue_key = get_key(linked_issue)
        link_type = link['type'][direction]

        if link_type in excludes:
            return linked_issue_key, None

        if direction == 'outward':
            log(issue_key + ' => ' + link_type + ' => ' + linked_issue_key)
        else:
            log(issue_key + ' <= ' + link_type + ' <= ' + linked_issue_key)

        extra = ""
        if link_type == "blocks":
            extra = ',color="red"'

        if direction not in show_directions:
            node = None
        else:
            node = '"%s"->"%s"[label="%s"%s]' % (issue_key, linked_issue_key, link_type, extra)

        return linked_issue, linked_issue_key, node

    # since the graph can be cyclic we need to prevent infinite recursion
    seen = []

    def walk(issue_key, graph):
        """ issue is the JSON representation of the issue """
        issue = jira.get_issue(issue_key)
        seen.append(issue_key)
        visit(graph, issue_key, issue, jira.url)

        children = []
        fields = issue['fields']
        if fields['issuetype']['name'] == 'Epic':
            issues = jira.query('"Epic Link" = "%s"' % issue_key)
            for subtask in issues:
                subtask_key = get_key(subtask)
                log(subtask_key + ' => references epic => ' + issue_key)
                node = '"%s"->"%s"[color=orange]' % (issue_key, subtask_key)
                graph.append(node)
                children.append(subtask_key)
        if fields.has_key('subtasks'):
            for subtask in fields['subtasks']:
                subtask_key = get_key(subtask)
                log(issue_key + ' => has subtask => ' + subtask_key)
                node = '"%s"->"%s"[color=blue][label="subtask"]' % (issue_key, subtask_key)
                graph.append(node)
                children.append(subtask_key)
        if fields.has_key('issuelinks'):
            for other_link in fields['issuelinks']:
                result = process_link(issue_key, other_link)
                if result is not None:
                    children.append(result[1])
                    if result[2] is not None: # add link
                        graph.append(result[2])

        # now construct graph data for all subtasks and links of this issue
        for child in (x for x in children if x not in seen):
            walk(child, graph)
        return graph

    return walk(start_issue_key, [])

def visit(graph, issue_key, issue, jira_url):
    global done_count
    global notdone_count

    if issue['fields']['status']['name'] in ['Closed', 'Resolved']:
        done_count += 1
        color = "grey"
    else:
        notdone_count += 1
        if issue['fields']['status']['name'] in ['Needs Information']:
            color = "red"
        else:
            color = "dodgerblue"

    summary = issue['fields']['summary']
    summary = summary.replace('"', '\\"')
    node = '"%s"[URL="%s/browse/%s",color="%s",tooltip="%s"]' % (issue_key, jira_url, issue_key, color, summary)
    graph.append(node)

def create_graph_image(graph_data, image_file):
    """ Given a formatted blob of graphviz chart data[1], make the actual request to Google
        and store the resulting image to disk.

        [1]: http://code.google.com/apis/chart/docs/gallery/graphviz.html
    """
    chart_url = GOOGLE_CHART_URL + 'cht=gv&chl=digraph{%s}' % ';'.join(graph_data)

    print('Google Chart request:')
    print(chart_url)

    response = requests.get(chart_url)

    with open(image_file, 'w+') as image:
        print('Writing to ' + image_file)
        image.write(response.content)

    return image_file


def submit_metrics_to_graphite(host, port, values=[]):
    sock = socket.socket(socket.AF_INET,
                         socket.SOCK_DGRAM)
    sock.connect((host, port))
    timestamp = int(time.time())
    for path, value in values:
        sock.send("%s %f %d\n" % (path, value, timestamp))
    sock.close()


def print_graph(graph_data):
    print('digraph{node [style=filled];%s}' % ';'.join(graph_data))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-u', '--user', dest='user', default='admin', help='Username to access JIRA')
    parser.add_argument('-p', '--password', dest='password', default='admin', help='Password to access JIRA')
    parser.add_argument('-c', '--cookie', dest='cookie', default=None, help='JSESSIONID session cookie value')
    parser.add_argument('-j', '--jira', dest='jira_url', default='http://jira.example.com', help='JIRA Base URL')
    parser.add_argument('-f', '--file', dest='image_file', default='issue_graph.png', help='Filename to write image to')
    parser.add_argument('-l', '--local', action='store_true', default=False, help='Render graphviz code to stdout')
    parser.add_argument('-x', '--exclude-link', dest='excludes', default=[], action='append', help='Exclude link type(s)')
    parser.add_argument('-s', '--show-directions', dest='show_directions', default=['inward', 'outward'], help='which directions to show (inward,outward)')
    parser.add_argument('-d', '--directions', dest='directions', default=['inward', 'outward'], help='which directions to walk (inward,outward)')
    parser.add_argument('-g', '--graphite-host', dest='graphite_host', default=None, help='report issue completion metrics to specified graphite hosts (host:port, can support multiple comma-separated hosts)')
    parser.add_argument('-P', '--graphite-port', dest='graphite_port', default=2003, help='report issue completion metrics to specified graphite host')
    parser.add_argument('-n', '--graphite-namespace-prefix', dest='graphite_namespace_prefix', default="jira", help='namespace prefix to use for graphite metrics')
    parser.add_argument('issue', nargs='?', help='The issue key (e.g. JRADEV-1107, JRADEV-1391)')

    return parser.parse_args()

done_count = 0
notdone_count = 0

def main():
    options = parse_args()

    if options.cookie is not None:
        # Log in with browser and use --cookie=ABCDEF012345 commandline argument
        auth = options.cookie
    else:
        # Basic Auth is usually easier for scripts like this to deal with than Cookies.
        auth = (options.user, options.password)

    jira = JiraSearch(options.jira_url, auth)

    graph = build_graph_data(options.issue, jira, options.excludes, options.show_directions, options.directions)

    if options.local:
        print_graph(graph)
    else:
        create_graph_image(graph, options.image_file)

    total_count = done_count + notdone_count
    log("%d of %d issues completed (%.1f%%)" % (done_count, total_count, 100 * done_count / (total_count * 1.0)))

    graphite_host = options.graphite_host
    graphite_port = int(options.graphite_port)

    if graphite_host is not None:
        graphite_namespace = "%s.%s" % (options.graphite_namespace_prefix, options.issue.lower())
        graphite_values = list(map(
            (lambda x: ("%s.%s.%s" % (options.graphite_namespace_prefix,
                                      options.issue.lower(),
                                      x[0]),
                        x[1],)),
            [("done", done_count),
             ("not-done", notdone_count),
             ("total", total_count),]))
        submit_metrics_to_graphite(graphite_host, graphite_port, graphite_values)

if __name__ == '__main__':
    main()
