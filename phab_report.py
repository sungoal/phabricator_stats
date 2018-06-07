#!/usr/bin/python

from datetime import datetime, timedelta
from jira import JIRA

import base64
import calendar
import copy
import json
import numpy
import math
import phabricator
import requests
import re
from flask import Flask, render_template, request
import os

tmpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
app = Flask(__name__, template_folder=tmpl_dir)

# Authentication Details for JIRA
AUTH = ("<JIRA_USER_NAME>", "<base64_encoded_jira_password>")

# JIRA CDM Issue URL
JIRA_URL = "<jira_browse_url>"

# Phabricator search string
SEARCH_STRING = "https://phabricator.<org_name>.com/D\d+"

class PhabricatorStats():
    def __init__(self, url, username, token):
        self.phabricator = phabricator.Phabricator(host=url, username=username,
                                                   token=token)
        self.phid_map = dict()
        self.report_dict = dict()
        self.auth = AUTH
        self.jira = JIRA(basic_auth=(self.auth[0],
                                     base64.b64decode(self.auth[1])),
                                     server='<JIRA_URL>')
        self.session  = requests.Session()
        self.session.auth = (self.auth[0], base64.b64decode(self.auth[1]))

    def create_username_phid_map(self, usernames):
        self.phid_map = self.phabricator.user.find(aliases=usernames)
        self.phid_map = dict(((self.phid_map[key], key) for key in self.phid_map))

    def find_authored_diffs(self, phids, last_n_days=None):
        diffs = self.phabricator.differential.query(authors=phids)
        if last_n_days:
            diffs_authored_last_n_days = list()
            end_date = datetime.now()
            start_date = end_date - timedelta(days=last_n_days)
            end_epoch = long(calendar.timegm(end_date.timetuple()))
            start_epoch = long(calendar.timegm(start_date.timetuple()))

            for diff in diffs:
                if not (long(diff["dateCreated"]) >= start_epoch and
                        long(diff["dateCreated"]) <= end_epoch):
                    continue
                if not diff["statusName"] in ["Closed", "Accepted"]:
                    diffs_authored_last_n_days.append(diff)
            del (diffs)
            return diffs_authored_last_n_days
        else:
            return diffs

    def find_closed_diffs(self, phids, last_n_days=None):
        closed_diffs = list()
        passed_status_names = ["Closed", "Accepted"]
        diffs = self.phabricator.differential.query(authors=phids)
        for diff in diffs:
            if last_n_days:
                end_date = datetime.now()
                start_date = end_date - timedelta(days=last_n_days)

                end_epoch = long(calendar.timegm(end_date.timetuple()))
                start_epoch = long(calendar.timegm(start_date.timetuple()))
                if not (long(diff["dateCreated"]) >= start_epoch and
                        long(diff["dateCreated"]) <= end_epoch):
                    continue
            if diff["statusName"] in passed_status_names:
                closed_diffs.append(diff)
        return closed_diffs

    def find_reviewed_diffs(self, phids, offset=0, limit=100, last_n_days=None,
                            comments_threshhold=2):
        reviewed_diffs_map = dict()
        diffs = self.phabricator.differential.query(reviewers=phids,
                                                    limit=limit, offset=offset)
        while len(diffs.response) > 0:
            for diff in diffs:
                diff_id = diff["id"]
                if last_n_days:
                    end_date = datetime.now()
                    start_date = end_date - timedelta(days=last_n_days)
                    end_epoch = long(calendar.timegm(end_date.timetuple()))
                    start_epoch = long(calendar.timegm(start_date.timetuple()))
                    if not (long(diff["dateCreated"]) >= start_epoch and
                            long(diff["dateCreated"]) <= end_epoch):
                        continue
                comments = self.phabricator.differential.getrevisioncomments(
                    ids=[int(diff_id)])[diff_id]
                comment_phids = copy.deepcopy(phids)
                comment_phid_dict = dict()
                for phid in comment_phids:
                    comment_phid_dict.setdefault(phid, dict())

                for comment in comments:
                    if len(comment_phids) == 0:
                        break
                    if comment["authorPHID"] in comment_phids:
		        if comment["authorPHID"] == diff["authorPHID"]:
                            continue
                        comment_phid_dict[comment["authorPHID"]].setdefault(
                            "comment_count", 0)
                        comment_phid_dict[comment["authorPHID"]][
                            "comment_count"] += 1
                        if comment_phid_dict[comment["authorPHID"]][
                            "comment_count"] == 1:
                            comment_phid_dict[comment["authorPHID"]][
                                "first_time_to_response"] = (
                                    long(comment["dateCreated"]) - long(
                                diff["dateCreated"]))
                        if comment_phid_dict[comment["authorPHID"]]["comment_count"] >= (
                                comments_threshhold):
                            reviewed_diffs_map.setdefault(
                                self.phid_map[comment["authorPHID"]], dict())
                            reviewed_diffs_map[self.phid_map[comment[
                                "authorPHID"]]].setdefault(diff["uri"], dict())
                            reviewed_diffs_map[
                                self.phid_map[comment["authorPHID"]]][diff[
                                "uri"]]["first_time_to_response"] = (
                            comment_phid_dict[
                                comment["authorPHID"]][
                                "first_time_to_response"])
                            comment_phids.remove(comment["authorPHID"])
                del (comment_phid_dict)
            offset = offset + limit
            diffs = self.phabricator.differential.query(reviewers=phids,
                                                        limit=limit,
                                                        offset=offset)
        return reviewed_diffs_map

    def __get_jira_issues_stats(self, jiras):
        jira_issue_type_dict = dict()
        for issue in jiras:
            jira_issue = self.jira.issue(issue)
            jira_issue_type_dict.setdefault(jira_issue.key, "Bug")
            if jira_issue.fields.issuetype == "Customer Bug":
                jira_issue_type_dict[jira_issue.key] = "Customer Bug"
        return jira_issue_type_dict

    def __is_backport_diff(self, diff, jiras):
        jira_diff_type_dict = dict()
        for issue in jiras:
            url = JIRA_URL+issue
            resp = self.session.get(url, verify=False) 
            content_to_search = resp.content.replace("\n", " | ")
            phabricator_links = re.findall(SEARCH_STRING, content_to_search)
            if phabricator_links:
                diff_ids = list()
                for phab_link in phabricator_links:
                    diff_id = phab_link.split("/")[-1][1:]
                    if diff_id == diff["id"]:
                        continue
                    else:
                        diff_ids.append(int(diff_id))
                if diff_ids:
                    jira_diffs = self.phabricator.differential.query(ids=diff_ids)
                    for jira_diff in jira_diffs:
                        if (jira_diff["diffs"] == diff["diffs"] and 
                            jira_diff["lineCount"] == diff["lineCount"]):
                            orig_diff_id = int(diff["id"])
                            jira_diff_id = int(jira_diff["id"])
                            if orig_diff_id < jira_diff_id:
                                return True
        return False
  
    def get_diff_stats(self, diffs):
        diff_stats_map = dict()
        for diff in diffs:
            username = self.phid_map[diff["authorPHID"]]
            diff_stats_map.setdefault(username, dict())
            diff_stats_map[username].setdefault(diff["uri"], dict())

            is_backport_diff = False

            if diff["statusName"] in ["Closed", "Accepted"]:
                diff_created_time = long(diff["dateCreated"])
                diff_closed_time = long(diff["dateModified"])
                time_to_closure = diff_closed_time - diff_created_time
            else:
                time_to_closure = None

            diff_line_count = diff["lineCount"]
            revision_count = len(diff["diffs"])
            jiras_associated = diff["auxiliary"]["jira.issues"]
            if jiras_associated:
                # Checking if the diff is a backport diff
                if self.__is_backport_diff(diff, jiras_associated):
                    is_backport_diff = True

                jira_issue_type_dict = (
                    self.__get_jira_issues_stats(
                    jiras_associated))
                diff_stats_map[username][diff["uri"]]["linked_jiras"] = (
                    jira_issue_type_dict)
            else:
                diff_stats_map[username][diff["uri"]]["linked_jiras"] = (
                    jiras_associated)

            diff_stats_map[username][diff["uri"]]["is_backport"] = (
                is_backport_diff)
            diff_stats_map[username][diff["uri"]]["closure_time"] = (
                time_to_closure)
            diff_stats_map[username][diff["uri"]]["line_count"] = (
                diff_line_count)
            diff_stats_map[username][diff["uri"]]["revision_count"] = (
                revision_count)
        return diff_stats_map

    def __get_mean_std_max(self, values):
        return {"mean": numpy.mean(values), "std": numpy.std(values),
                "max": numpy.max(values)}

    def __get_report_data(self, diff_dict, report_type=None):
        report_dict = dict()
        for author in diff_dict:
            report_dict.setdefault(author, dict())
            total_diffs = len(diff_dict[author].keys())
            closure_time = list()
            line_count = list()
            revision_count = list()
            jiras_resolved = set()
            for diff_uri in diff_dict[author]:
                # Eliminating the backport diffs from the authored/closed count
                if report_type:
                    if diff_dict[author][diff_uri]["is_backport"]:
                        continue
                closure_time.append(diff_dict[author][diff_uri]["closure_time"])
                line_count.append(
                    int(diff_dict[author][diff_uri]["line_count"]))
                revision_count.append(
                    int(diff_dict[author][diff_uri]["revision_count"]))
                jiras_resolved.update(
                    diff_dict[author][diff_uri]["linked_jiras"])

            if report_type == "closed":
                closure_time = self.__get_mean_std_max(closure_time)
                for k in closure_time:
                    closure_time[k] = str(timedelta(seconds=closure_time[k]))
            else:
                closure_time = None

            line_count = self.__get_mean_std_max(line_count)
            for k in line_count:
                line_count[k] = math.floor(line_count[k])

            revision_count = self.__get_mean_std_max(revision_count)
            for k in revision_count:
                revision_count[k] = math.floor(revision_count[k])


            if report_type == "closed":
                report_dict[author]["closed_diffs"] = total_diffs
            if report_type == "authored":
                report_dict[author]["authored_diffs"] = total_diffs

            report_dict[author]["closure_time_per_diff"] = closure_time
            report_dict[author]["revision_count_per_diff"] = revision_count
            report_dict[author]["line_count_per_diff"] = line_count

            if report_type == "closed":
                report_dict[author]["jiras_resolved"] = list(jiras_resolved)
            if report_type == "authored":
                report_dict[author]["jiras_in_progress"] = list(jiras_resolved)

        return report_dict

    def generate_report(self, phids, last_n_days=7):
        reviewed_diffs = self.find_reviewed_diffs(phids,
                                                  last_n_days=last_n_days)
        closed_diffs = self.find_closed_diffs(phids, last_n_days=last_n_days)
        authored_diffs = self.find_authored_diffs(phids,
                                                  last_n_days=last_n_days)

        closed_diffs_stats = self.get_diff_stats(closed_diffs)
        authored_diff_stats = self.get_diff_stats(authored_diffs)
        closed_report_diff_dict = self.__get_report_data(closed_diffs_stats,
                                                         report_type="closed")
        authored_report_diff_dict = self.__get_report_data(authored_diff_stats,
                                                           report_type="authored")

        for author in closed_report_diff_dict:
            self.report_dict.setdefault(author, dict())
            self.report_dict[author]["closed"] = closed_report_diff_dict[author]

        for author in authored_report_diff_dict:
            self.report_dict.setdefault(author, dict())
            self.report_dict[author]["authored"] = authored_report_diff_dict[
                author]

        # Deleting as these are no longer required.
        del (closed_report_diff_dict)
        del (authored_report_diff_dict)

        for author in reviewed_diffs:
            time_to_respond = list()
            total_reviewed_diffs = len(reviewed_diffs[author].keys())
            for uri in reviewed_diffs[author]:
                time_to_respond.append(
                    reviewed_diffs[author][uri]["first_time_to_response"])
            time_to_respond = self.__get_mean_std_max(time_to_respond)

            for k in time_to_respond:
                time_to_respond[k] = math.floor(time_to_respond[k]/3600)

            if self.report_dict.has_key(author):
                self.report_dict[author].setdefault("reviewed", dict())
                self.report_dict[author]["reviewed"][
                    "reviewed_diffs"] = total_reviewed_diffs
                self.report_dict[author]["reviewed"][
                    "first_time_to_respond"] = time_to_respond
            else:
                self.report_dict[author] = dict()
                self.report_dict[author].setdefault("reviewed", dict())
                self.report_dict[author]["reviewed"][
                    "reviewed_diffs"] = total_reviewed_diffs
                self.report_dict[author]["reviewed"][
                    "first_time_to_respond"] = time_to_respond

phab = PhabricatorStats("<phabricator_url>", "<username>", "<token>")

def fetch_report():
    phab.create_username_phid_map(["<phabricator_user_ids>"])
    phids = phab.phid_map.keys()
    phab.generate_report(phids, last_n_days=7)
    with open("phabricator_stats.json", "w") as FW:
        json.dump(phab.report_dict, FW)


@app.route("/report/", methods=['GET', 'POST'])
def report():
    if request.method == 'POST' or not phab.report_dict:
        fetch_report()
    return render_template('phab.html',data=phab.report_dict)


@app.route("/pie/", methods=['GET', 'POST'])
def pie_report():
    if request.method == 'POST' or not phab.report_dict:
        fetch_report()
    return render_template('phab_pie.html',data=phab.report_dict)


@app.route("/person/", defaults={'user': None})
@app.route("/person/<user>")
def person(user):
    if not phab.report_dict:
        fetch_report()
    if user:
        return render_template('person.html', data={user: phab.report_dict[user]})
    return render_template('person.html', data=phab.report_dict)


if __name__ == "__main__":
    app.run()

