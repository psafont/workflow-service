from __future__ import print_function
import os

from json import dumps
from future.utils import iteritems

from flask import (
    Response, request, redirect, abort, send_from_directory, jsonify
)

from aap_client.flask.decorators import jwt_optional, jwt_required, get_user

import workflow_service.make_enum_json_serializable  # pylint: disable=W0611

from workflow_service import JOBS, JOBS_LOCK, USER_OWNS, JOBS_OWNED_BY, app
from workflow_service.decorators import job_exists, user_is_authorized
from workflow_service.job_runner import JobRunner

APP = app()


# changes location from local filesystem to flask endpoint URL
def url_location(url_root):
    def change_locations(job):
        # replace locations in the outputs so web clients can retrieve all
        # the outputs
        change_all_locations(
            job.output(),
            url_root[:-1] + u'/jobs/' + str(job.jobid()) + u'/output')
    return change_locations


def change_all_locations(obj, url_name):
    if isinstance(obj, list):
        for o_name, output in enumerate(obj):
            change_all_locations(output, url_name + u'/' + str(o_name))
    elif isinstance(obj, dict):
        if 'location' not in obj:
            for o_name, output in iteritems(obj):
                change_all_locations(output, url_name + u'/' + o_name)
        else:
            obj[u'location'] = url_name
    else:
        APP.logger.error(
            u'Couldn\'t process output "%s" to change the locations', url_name)


@APP.errorhandler(404)
def page_not_found(error):
    return jsonify(error=404, text=str(error)), 404


@APP.errorhandler(500)
def badaboom(error):
    APP.logger.error(error)
    return (
        jsonify(
            error=500,
            text=u'Internal server error, please contact the administrator.'),
        500
    )


@APP.route(u'/health', methods=[u'GET'])
def healthcheck():
    return Response('It\'s alive!')


@APP.route(u'/run', methods=[u'POST'])
@jwt_optional
def run_workflow():
    path = request.args[u'wf']
    current_user = get_user()
    oncompletion = url_location(request.url_root)
    body = request.stream.read()

    with JOBS_LOCK:
        job = JobRunner(path, body, request.url_root,
                        oncompletion=oncompletion, owner=current_user)
        jobid = job.jobid()
        JOBS[jobid] = job

    if current_user:  # non-anonymous user
        USER_OWNS[jobid] = current_user
        JOBS_OWNED_BY[current_user] =\
            JOBS_OWNED_BY.get(current_user, []) + [jobid]
    job.start()
    return redirect(u'/jobs/{}'.format(jobid), code=303)


@APP.route(u'/jobs/<string:jobid>',
           methods=[u'GET', u'POST'],
           strict_slashes=False)
@jwt_optional
@job_exists
@user_is_authorized
def job_control(jobid):
    job = getjob(jobid)

    if request.method == u'POST':
        action = request.args.get(u'action')
        if action:
            if action == u'cancel':
                job.cancel()
            elif action == u'pause':
                job.pause()
            elif action == u'resume':
                job.resume()

    return dumps(job.status()), 200, u''


@APP.route(u'/jobs/<string:jobid>/log', methods=[u'GET'])
@jwt_optional
@job_exists
@user_is_authorized
def get_log(jobid):
    job = getjob(jobid)
    return Response(job.logspooler(), mimetype='text/event-stream')


@APP.route(u'/jobs/<string:jobid>/output/<path:outputid>', methods=[u'GET'])
@jwt_optional
@job_exists
@user_is_authorized
def get_output(jobid, outputid):
    job = getjob(jobid)

    output = getoutputobj(job.status(), outputid)
    if not output or not isfile(output):
        return abort(404)

    (path, filename), wf_filename = getfile(output)
    if not path or not filename:
        return abort(404)

    return send_from_directory(path, filename, attachment_filename=wf_filename)


@APP.route(u'/jobs', methods=[u'GET'], strict_slashes=False)
@jwt_required
def get_jobs():
    job_ids = JOBS_OWNED_BY.get(get_user(), [])
    jobs = []
    with JOBS_LOCK:
        for job_id in job_ids:
            jobs.append(JOBS[job_id])
    return Response(spool(jobs))


def getoutputobj(status, outputid):
    path = outputid.split(u'/')
    try:
        ref = status[u'output']
        for key in path:
            if isinstance(ref, list):
                key = int(key)
            ref = ref[key]

        return ref
    except (KeyError, TypeError, ValueError, IndexError) as err:
        APP.logger.info(
            u'Couldn\'t retrieve output "%s" because %s', outputid, str(err))
        return None


def isfile(obj):
    return isinstance(obj, dict) and u'path' in obj and u'basename' in obj


def getfile(file_dict):
    return os.path.split(file_dict[u'path']), file_dict[u'basename']


def getjob(jobid):
    with JOBS_LOCK:
        job = JOBS[jobid]
    return job


def spool(jobs):
    yield u'['
    connector = u''
    for job in jobs:
        yield connector + dumps(job.status())
        if connector == u'':
            connector = u', '
    yield u']'


def main():
    # APP.debug = True
    APP.run(u'0.0.0.0')


if __name__ == u'__main__':
    main()
