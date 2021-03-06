import os
import random

from urlparse import urlparse
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.contrib.auth.models import User
from django.conf import settings
from django.http import (
    HttpResponseBadRequest, HttpResponseRedirect, HttpResponseForbidden)
from django.utils.http import urlencode
from django.utils.translation import ugettext as _
from django.utils import six
from django.utils import timezone
from django.db import IntegrityError
from django.db.models import Prefetch

from pyxform.xls2json import parse_file_to_json
from pyxform.builder import create_survey_element_from_dict
from rest_framework import exceptions
from rest_framework import status
from rest_framework.decorators import detail_route, list_route
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.viewsets import ModelViewSet
from rest_framework.exceptions import ParseError
from rest_framework.filters import DjangoFilterBackend

from onadata.apps.logger.xform_instance_parser import XLSFormError
from onadata.apps.main.views import get_enketo_preview_url
from onadata.apps.api import tasks
from onadata.libs import filters, authentication
from onadata.libs.mixins.anonymous_user_public_forms_mixin import (
    AnonymousUserPublicFormsMixin)
from onadata.libs.mixins.authenticate_header_mixin import \
    AuthenticateHeaderMixin
from onadata.libs.mixins.labels_mixin import LabelsMixin
from onadata.libs.mixins.cache_control_mixin import CacheControlMixin
from onadata.libs.mixins.etags_mixin import ETagsMixin
from onadata.libs.renderers import renderers
from onadata.libs.serializers.xform_serializer import (
    XFormBaseSerializer, XFormSerializer, XFormCreateSerializer)
from onadata.libs.serializers.clone_xform_serializer import \
    CloneXFormSerializer
from onadata.libs.serializers.share_xform_serializer import (
    ShareXFormSerializer)
from onadata.apps.api import tools as utils
from onadata.apps.api.permissions import XFormPermissions
from onadata.apps.logger.models.xform import XForm
from onadata.apps.logger.models.xform import XFormUserObjectPermission
from onadata.libs.utils.viewer_tools import (
    enketo_url,
    EnketoError,
    generate_enketo_form_defaults)
from onadata.libs.utils.logger_tools import publish_form
from onadata.libs.utils.string import str2bool

from onadata.libs.utils.csv_import import get_async_csv_submission_status
from onadata.libs.utils.csv_import import submit_csv
from onadata.libs.utils.csv_import import submit_csv_async
from onadata.libs.utils.viewer_tools import get_form_url
from onadata.libs.utils.api_export_tools import custom_response_handler
from onadata.libs.utils.api_export_tools import process_async_export
from onadata.libs.utils.api_export_tools import get_async_response
from onadata.libs.utils.api_export_tools import response_for_format
from onadata.libs.utils.export_tools import parse_request_export_options
from onadata.apps.api.tools import get_baseviewset_class


BaseViewset = get_baseviewset_class()


def upload_to_survey_draft(filename, username):
    return os.path.join(
        username,
        'survey-drafts',
        os.path.split(filename)[1]
    )


def get_survey_dict(csv_name):
    survey_file = default_storage.open(csv_name, 'r')
    survey_dict = parse_file_to_json(
        survey_file.name, default_name='data', file_object=survey_file)

    return survey_dict


def _get_user(username):
    users = User.objects.filter(username__iexact=username)

    return users.count() and users[0] or None


def _get_owner(request):
    owner = request.data.get('owner') or request.user

    if isinstance(owner, six.string_types):
        owner_obj = _get_user(owner)

        if owner_obj is None:
            raise ValidationError(
                u"User with username %s does not exist." % owner)
        else:
            owner = owner_obj

    return owner


def value_for_type(form, field, value):
    if form._meta.get_field(field).get_internal_type() == 'BooleanField':
        return str2bool(value)

    return value


def _try_update_xlsform(request, xform, owner):
    survey = \
        utils.publish_xlsform(request, owner, xform.id_string, xform.project)

    if isinstance(survey, XForm):
        serializer = XFormSerializer(
            xform, context={'request': request})

        return Response(serializer.data, status=status.HTTP_200_OK)

    return Response(survey, status=status.HTTP_400_BAD_REQUEST)


def result_has_error(result):
    return isinstance(result, dict) and result.get('type')


def get_survey_xml(csv_name):
    survey_dict = get_survey_dict(csv_name)
    survey = create_survey_element_from_dict(survey_dict)
    return survey.to_xml()


def set_enketo_signed_cookies(resp, username=None, json_web_token=None):
    if not username and not json_web_token:
        return

    max_age = 30 * 24 * 60 * 60 * 1000

    __enketo_meta_uid = {'max_age': max_age, 'salt': settings.ENKETO_API_SALT}
    __enketo = {'secure': False, 'salt': settings.ENKETO_API_SALT}

    # add domain attribute if ENKETO_AUTH_COOKIE_DOMAIN is set in settings
    # i.e. don't add in development environment because cookie automatically
    # assigns 'localhost' as domain
    if getattr(settings, 'ENKETO_AUTH_COOKIE_DOMAIN', None):
        __enketo_meta_uid['domain'] = settings.ENKETO_AUTH_COOKIE_DOMAIN
        __enketo['domain'] = settings.ENKETO_AUTH_COOKIE_DOMAIN

    resp.set_signed_cookie('__enketo_meta_uid', username, **__enketo_meta_uid)
    resp.set_signed_cookie('__enketo', json_web_token, **__enketo)

    return resp


def parse_webform_return_url(return_url, request):
    """
    Given a webform url and request containing authentication information
    extract authentication data encoded in the url and validate using either
    this data or data in the request. Construct a proper return URL, which has
    stripped the authentication data, to return the user.
    """
    jwt_param = None
    url = urlparse(return_url)
    try:
        # get jwt from url - probably zebra via enketo
        jwt_param = filter(
            lambda p: p.startswith('jwt'),
            url.query.split('&'))
        jwt_param = jwt_param and jwt_param[0].split('=')[1]

        if not jwt_param:
            return
    except IndexError:
        pass

    if '/_/' in return_url:  # offline url
        redirect_url = "%s://%s%s#%s" % (
            url.scheme, url.netloc, url.path, url.fragment)
    elif '/::' in return_url:  # non-offline url
        redirect_url = "%s://%s%s" % (url.scheme, url.netloc, url.path)
    else:
        # unexpected format
        return

    response_redirect = HttpResponseRedirect(redirect_url)

    # if the requesting user is not authenticated but the token has been
    # retrieved from the url - probably zebra via enketo express - use the
    # token to create signed cookies which will be used by subsequent
    # enketo calls to authenticate the user
    if jwt_param:
        if request.user.is_anonymous():
            api_token = authentication.get_api_token(jwt_param)
            if getattr(api_token, 'user'):
                username = api_token.user.username
        else:
            username = request.user.username

        response_redirect = set_enketo_signed_cookies(
            response_redirect, username=username, json_web_token=jwt_param)

        return response_redirect


class XFormViewSet(AnonymousUserPublicFormsMixin,
                   AuthenticateHeaderMixin,
                   CacheControlMixin,
                   ETagsMixin,
                   LabelsMixin,
                   BaseViewset,
                   ModelViewSet):
    """
    Publish XLSForms, List, Retrieve Published Forms.
    """

    renderer_classes = api_settings.DEFAULT_RENDERER_CLASSES + [
        renderers.XLSRenderer,
        renderers.XLSXRenderer,
        renderers.CSVRenderer,
        renderers.CSVZIPRenderer,
        renderers.SAVZIPRenderer,
        renderers.SurveyRenderer,
        renderers.OSMExportRenderer,
        renderers.ZipRenderer,
        renderers.GoogleSheetsRenderer
    ]
    queryset = XForm.objects.select_related('user', 'created_by')\
        .prefetch_related(
            Prefetch(
                'xformuserobjectpermission_set',
                queryset=XFormUserObjectPermission.objects.select_related(
                    'user__profile__organizationprofile',
                    'permission'
                )
            ),
            Prefetch('metadata_set'),
            Prefetch('tags'),
            Prefetch('dataview_set')
        )
    serializer_class = XFormSerializer
    lookup_field = 'pk'
    extra_lookup_fields = None
    permission_classes = [XFormPermissions, ]
    updatable_fields = set(('description', 'downloadable', 'require_auth',
                            'shared', 'shared_data', 'title'))
    filter_backends = (filters.AnonDjangoObjectPermissionFilter,
                       filters.TagFilter,
                       filters.XFormOwnerFilter,
                       DjangoFilterBackend)
    filter_fields = ('instances_with_osm',)

    public_forms_endpoint = 'public'

    def get_serializer_class(self):
        if self.action == 'list':
            return XFormBaseSerializer

        return super(XFormViewSet, self).get_serializer_class()

    def create(self, request, *args, **kwargs):
        try:
            owner = _get_owner(request)
        except ValidationError as e:
            return Response({'message': e.messages[0]},
                            status=status.HTTP_400_BAD_REQUEST)

        survey = utils.publish_xlsform(request, owner)
        if isinstance(survey, XForm):
            serializer = XFormCreateSerializer(
                survey, context={'request': request})
            headers = self.get_success_headers(serializer.data)

            return Response(serializer.data, status=status.HTTP_201_CREATED,
                            headers=headers)

        return Response(survey, status=status.HTTP_400_BAD_REQUEST)

    @list_route(methods=['POST', 'GET'])
    def create_async(self, request, *args, **kwargs):
        """ Temporary Endpoint for Async form creation """
        resp = headers = {}
        resp_code = status.HTTP_400_BAD_REQUEST

        if request.method == 'GET':
            self.etag_data = '{}'.format(timezone.now())
            survey = tasks.get_async_status(
                request.query_params.get('job_uuid'))

            if 'pk' in survey:
                xform = XForm.objects.get(pk=survey.get('pk'))
                serializer = XFormSerializer(
                    xform, context={'request': request})
                headers = self.get_success_headers(serializer.data)
                resp = serializer.data
                resp_code = status.HTTP_201_CREATED
            else:
                resp_code = status.HTTP_202_ACCEPTED
                resp.update(survey)
        else:
            try:
                owner = _get_owner(request)
            except ValidationError as e:
                return Response({'message': e.messages[0]},
                                status=status.HTTP_400_BAD_REQUEST)

            fname = request.FILES.get('xls_file').name
            resp.update(
                {u'job_uuid':
                 tasks.publish_xlsform_async.delay(
                     request.user, request.POST, owner,
                     ({'name': fname,
                       'data': request.FILES.get('xls_file').read()}
                      if isinstance(request.FILES.get('xls_file'),
                                    InMemoryUploadedFile) else
                      {'name': fname,
                       'path': request.FILES.get(
                           'xls_file').temporary_file_path()})).task_id})
            resp_code = status.HTTP_202_ACCEPTED

        return Response(data=resp, status=resp_code, headers=headers)

    @detail_route()
    def form(self, request, format='json', **kwargs):
        form = self.get_object()
        if format not in ['json', 'xml', 'xls']:
            return HttpResponseBadRequest('400 BAD REQUEST',
                                          content_type='application/json',
                                          status=400)
        self.etag_data = '{}'.format(form.date_modified)
        filename = form.id_string + "." + format
        response = response_for_format(form, format=format)
        response['Content-Disposition'] = 'attachment; filename=' + filename

        return response

    @list_route(methods=['GET'])
    def login(self, request, **kwargs):
        return_url = request.query_params.get('return')

        if return_url:
            redirect = parse_webform_return_url(return_url, request)

            if redirect:
                return redirect

            login_vars = {"login_url": settings.ENKETO_CLIENT_LOGIN_URL,
                          "return_url": urlencode({'return_url': return_url})}
            client_login = '{login_url}?{return_url}'.format(**login_vars)

            return HttpResponseRedirect(client_login)

        return HttpResponseForbidden(
            "Authentication failure, cannot redirect")

    @detail_route()
    def enketo(self, request, **kwargs):
        self.object = self.get_object()
        form_url = get_form_url(
            request, self.object.user.username, settings.ENKETO_PROTOCOL)

        data = {'message': _(u"Enketo not properly configured.")}
        http_status = status.HTTP_400_BAD_REQUEST

        try:
            # pass default arguments to enketo_url to prepopulate form fields
            request_vars = request.GET
            defaults = generate_enketo_form_defaults(
                self.object, **request_vars)
            url = enketo_url(form_url, self.object.id_string, **defaults)
            preview_url = get_enketo_preview_url(request,
                                                 self.object.user.username,
                                                 self.object.id_string)
        except EnketoError as e:
            data = {'message': _(u"Enketo error: %s" % e)}
        else:
            if url and preview_url:
                http_status = status.HTTP_200_OK
                data = {"enketo_url": url, "enketo_preview_url": preview_url}

        return Response(data, http_status)

    @list_route(methods=['POST', 'GET'])
    def survey_preview(self, request, **kwargs):
        username = request.user.username
        if request.method.upper() == 'POST':
            if not username:
                raise ParseError("User has to be authenticated")

            csv_data = request.data.get('body')
            if csv_data:
                rand_name = "survey_draft_%s.csv" % ''.join(
                    random.sample("abcdefghijklmnopqrstuvwxyz0123456789", 6))
                csv_file = ContentFile(csv_data)
                csv_name = default_storage.save(
                    upload_to_survey_draft(rand_name, username),
                    csv_file)

                result = publish_form(lambda: get_survey_xml(csv_name))

                if result_has_error(result):
                    raise ParseError(result.get('text'))

                return Response(
                    {'unique_string': rand_name, 'username': username},
                    status=200)
            else:
                raise ParseError('Missing body')

        if request.method.upper() == 'GET':
            filename = request.query_params.get('filename')
            username = request.query_params.get('username')

            if not username:
                raise ParseError('Username not provided')
            if not filename:
                raise ParseError("Filename MUST be provided")

            csv_name = upload_to_survey_draft(filename, username)

            result = publish_form(lambda: get_survey_xml(csv_name))

            if result_has_error(result):
                raise ParseError(result.get('text'))

            self.etag_data = result

            return Response(result, status=200)

    def retrieve(self, request, *args, **kwargs):
        lookup_field = self.lookup_field
        lookup = self.kwargs.get(lookup_field)

        if lookup == self.public_forms_endpoint:
            self.object_list = self._get_public_forms_queryset()

            page = self.paginate_queryset(self.object_list)
            if page is not None:
                serializer = self.get_pagination_serializer(page)
            else:
                serializer = self.get_serializer(self.object_list, many=True)

            return Response(serializer.data)

        xform = self.get_object()
        export_type = kwargs.get('format') or \
            request.query_params.get('format')
        query = request.query_params.get("query", {})
        token = request.GET.get('token')
        meta = request.GET.get('meta')

        if export_type is None or export_type in ['json']:
            # perform default viewset retrieve, no data export
            return super(XFormViewSet, self).retrieve(request, *args, **kwargs)

        return custom_response_handler(request,
                                       xform,
                                       query,
                                       export_type,
                                       token,
                                       meta)

    @detail_route(methods=['POST'])
    def share(self, request, *args, **kwargs):
        self.object = self.get_object()

        data = {}
        for key, val in request.data.iteritems():
            data[key] = val
        data.update({'xform': self.object.pk})

        serializer = ShareXFormSerializer(data=data)

        if serializer.is_valid():
            serializer.save()
        else:
            return Response(data=serializer.errors,
                            status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)

    @detail_route(methods=['POST'])
    def clone(self, request, *args, **kwargs):
        self.object = self.get_object()
        data = {'xform': self.object.pk,
                'username': request.data.get('username')}
        project = request.data.get('project_id')
        if project:
            data['project'] = project
        serializer = CloneXFormSerializer(data=data)
        if serializer.is_valid():
            clone_to_user = User.objects.get(username=data['username'])
            if not request.user.has_perm('can_add_xform',
                                         clone_to_user.profile):
                raise exceptions.PermissionDenied(
                    detail=_(u"User %(user)s has no permission to add "
                             "xforms to account %(account)s" %
                             {'user': request.user.username,
                              'account': data['username']}))
            try:
                xform = serializer.save()
            except IntegrityError:
                raise ParseError(
                    'A clone with the same id_string has already been created')
            serializer = XFormSerializer(
                xform.cloned_form, context={'request': request})

            return Response(data=serializer.data,
                            status=status.HTTP_201_CREATED)

        return Response(data=serializer.errors,
                        status=status.HTTP_400_BAD_REQUEST)

    @detail_route(methods=['POST', 'GET'])
    def csv_import(self, request, *args, **kwargs):
        """ Endpoint for CSV data imports
        Calls :py:func:`onadata.libs.utils.csv_import.submit_csv` for POST
        requests passing the `request.FILES.get('csv_file')` upload
        for import and
        :py:func:onadata.libs.utils.csv_import.get_async_csv_submission_status
        for GET requests passing `job_uuid` query param for job progress
        polling
        """
        self.object = self.get_object()
        resp = {}
        if request.method == 'GET':
            try:
                resp.update(get_async_csv_submission_status(
                    request.query_params.get('job_uuid')))
                self.last_modified_date = timezone.now()
            except ValueError:
                raise ParseError(('The instance of the result is not a '
                                  'basestring; the job_uuid variable might '
                                  'be incorrect'))
        else:
            csv_file = request.FILES.get('csv_file', None)
            if csv_file is None:
                resp.update({u'error': u'csv_file field empty'})
            else:
                num_rows = sum(1 for row in csv_file) - 1
                if num_rows < settings.CSV_ROW_IMPORT_ASYNC_THRESHOLD:
                    resp.update(submit_csv(request.user.username,
                                           self.object, csv_file))
                else:
                    tmp_file_path = utils.generate_tmp_path(csv_file)
                    task = submit_csv_async.delay(request.user.username,
                                                  self.object,
                                                  tmp_file_path)
                    if task is None:
                        raise ParseError('Task not found')
                    else:
                        resp.update({u'task_id': task.task_id})

        return Response(
            data=resp,
            status=status.HTTP_200_OK if resp.get('error') is None else
            status.HTTP_400_BAD_REQUEST)

    def partial_update(self, request, *args, **kwargs):
        self.object = self.get_object()
        owner = self.object.user

        # updating the file
        if request.FILES or 'xls_url' in request.data \
                or 'dropbox_xls_url' in request.data:
            return _try_update_xlsform(request, self.object, owner)

        return super(XFormViewSet, self).partial_update(request, *args,
                                                        **kwargs)

    @detail_route(methods=['DELETE', 'GET'])
    def delete_async(self, request, *args, **kwargs):
        if request.method == 'DELETE':
            xform = self.get_object()
            xform.soft_delete()
            resp = {
                u'job_uuid': tasks.delete_xform_async.delay(xform).task_id,
                u'time_async_triggered': datetime.now()}
            resp_code = status.HTTP_202_ACCEPTED

        elif request.method == 'GET':
            job_uuid = request.query_params.get('job_uuid')
            resp = tasks.get_async_status(job_uuid)
            resp_code = status.HTTP_202_ACCEPTED
            self.etag_data = '{}'.format(timezone.now())

        return Response(data=resp, status=resp_code)

    @detail_route(methods=['GET'])
    def export_async(self, request, *args, **kwargs):
        job_uuid = request.query_params.get('job_uuid')
        export_type = request.query_params.get('format')
        query = request.query_params.get("query")
        xform = self.get_object()

        token = request.query_params.get('token')
        meta = request.query_params.get('meta')
        data_id = request.query_params.get('data_id')
        options = parse_request_export_options(request.query_params)

        options.update({
            'meta': meta,
            'token': token,
            'data_id': data_id,
            'query': query,
        })

        if job_uuid:
            resp = get_async_response(job_uuid, request, xform)
        else:
            resp = process_async_export(request, xform, export_type, options)

            if isinstance(resp, HttpResponseRedirect):
                payload = {
                    "details": _("Google authorization needed"),
                    "url": resp.url
                }
                return Response(data=payload,
                                status=status.HTTP_403_FORBIDDEN,
                                content_type="application/json")

        self.etag_data = '{}'.format(timezone.now())

        return Response(data=resp,
                        status=status.HTTP_202_ACCEPTED,
                        content_type="application/json")

    def list(self, request, *args, **kwargs):
        try:
            queryset = self.filter_queryset(self.get_queryset())
            last_modified = queryset.values_list('date_modified', flat=True)\
                .order_by('-date_modified')
            if last_modified:
                self.etag_data = last_modified[0]
            resp = super(XFormViewSet, self).list(request, *args, **kwargs)
        except XLSFormError, e:
            resp = HttpResponseBadRequest(e.message)

        return resp
