# -*- test-case-name: admin.test.test_release -*-
# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Helper utilities for the Flocker release process.
"""

import sys
from collections import namedtuple
from effect import (
    Effect, sync_perform, ComposedDispatcher, base_dispatcher)
from effect.do import do, do_return


from twisted.python.usage import Options, UsageError

import flocker


# TODO: Get this from https://github.com/ClusterHQ/flocker/pull/1092
from flocker.docs import get_doc_version, is_release, is_weekly_release

from ..aws import (
    boto_dispatcher,
    UpdateS3RoutingRule,
    ListS3Keys,
    DeleteS3Keys,
    CopyS3Keys,
    CreateCloudFrontInvalidation,
)


__all__ = ['rpm_version', 'make_rpm_version']

# Use characteristic instead.
# https://clusterhq.atlassian.net/browse/FLOC-1223
rpm_version = namedtuple('rpm_version', 'version release')


def make_rpm_version(flocker_version):
    """
    Parse the Flocker version generated by versioneer into an RPM compatible
    version and a release version.
    See: http://fedoraproject.org/wiki/Packaging:NamingGuidelines#Pre-Release_packages  # noqa

    :param flocker_version: The versioneer style Flocker version string.
    :return: An ``rpm_version`` tuple containing a ``version`` and a
        ``release`` attribute.
    """
    # E.g. 0.1.2-69-gd2ff20c-dirty
    # tag+distance+shortid+dirty
    parts = flocker_version.split('-')
    tag, remainder = parts[0], parts[1:]
    for suffix in ('pre', 'dev'):
        parts = tag.rsplit(suffix, 1)
        if len(parts) == 2:
            # A pre or dev suffix was present. ``version`` is the part before
            # the pre and ``suffix_number`` is the part after the pre, but
            # before the first dash.
            version = parts.pop(0)
            suffix_number = parts[0]
            if suffix_number.isdigit():
                # Given pre or dev number X create a 0 prefixed, `.` separated
                # string of version labels. E.g.
                # 0.1.2pre2  becomes
                # 0.1.2-0.pre.2
                release = ['0', suffix, suffix_number]
            else:
                # Non-integer pre or dev number found.
                raise Exception(
                    'Non-integer value "{}" for "{}". '
                    'Supplied version {}'.format(
                        suffix_number, suffix, flocker_version))
            break
    else:
        # Neither of the expected suffixes was found, the tag can be used as
        # the RPM version
        version = tag
        release = ['1']

    if remainder:
        # The version may also contain a distance, shortid which
        # means that there have been changes since the last
        # tag. Additionally there may be a ``dirty`` suffix which
        # indicates that there are uncommitted changes in the
        # working directory.  We probably don't want to release
        # untagged RPM versions, and this branch should probably
        # trigger and error or a warning. But for now we'll add
        # that extra information to the end of release number.
        # See https://clusterhq.atlassian.net/browse/FLOC-833
        release.extend(remainder)

    return rpm_version(version, '.'.join(release))


class NotARelease(Exception):
    """
    Raised when trying
    """


def configure_s3_routing_rules(doc_version, bucket, is_dev):
    prefix = 'en/devel/' if is_dev else 'en/latest/'
    target_prefix = '/en/%s/' % (doc_version,)
    return Effect(UpdateS3RoutingRule(
        bucket=bucket,
        prefix=prefix,
        target_prefix=target_prefix))


@do
def create_cloudfront_invalidation(doc_version, bucket, is_dev,
                                   changed_keys, old_prefix):
    if is_dev:
        prefixes = ["/en/devel/"]
    else:
        prefixes = ["/en/latest/"]
    prefixes += ["/en/%s/" % (doc_version,)]

    if old_prefix:
        list_old_keys = Effect(ListS3Keys(
            bucket=bucket, prefix=old_prefix[1:])).on(success=set)
        changed_keys |= yield list_old_keys

    for index in ['index.html', '/index.html']:
        changed_keys |= {key_name[:-len(index)]
                         for key_name in changed_keys
                         if key_name.endswith(index)}

    paths = [prefix + key_name
             for key_name in changed_keys
             for prefix in prefixes]

    cname = 'from_bucket' + bucket
    yield do_return(
        Effect(CreateCloudFrontInvalidation(cname=cname, paths=paths))
    )


@do
def copy_docs(flocker_version, doc_version, bucket):
    destination_bucket = bucket
    source_bucket = 'clusterhq-dev-docs'
    source_prefix = '%s/' % (flocker_version,)
    destination_prefix = 'en/%s/' % (doc_version,)

    source_keys = yield Effect(ListS3Keys(bucket=source_bucket,
                                          prefix=source_prefix)
                               ).on(success=set)
    destination_keys = yield Effect(ListS3Keys(bucket=destination_bucket,
                                               prefix=destination_prefix)
                                    ).on(success=set)

    keys_to_delete = destination_keys - source_keys
    yield Effect(DeleteS3Keys(bucket=destination_bucket,
                              prefix=destination_prefix,
                              keys=keys_to_delete))

    yield Effect(CopyS3Keys(source_bucket=source_bucket,
                            source_prefix=source_prefix,
                            destination_bucket=destination_bucket,
                            destination_prefix=destination_prefix,
                            keys=keys_to_delete))

    changed_keys = destination_keys | source_keys

    yield do_return(
        changed_keys
    )


@do
def publish_docs(flocker_version, doc_version, bucket):
    changed_keys = yield copy_docs(flocker_version, doc_version, bucket)

    # Wether the latest, or the devel link should be updated.
    is_devel = not is_release(doc_version)
    old_prefix = yield configure_s3_routing_rules(
        doc_version=doc_version,
        bucket=bucket,
        is_dev=is_devel)
    yield create_cloudfront_invalidation(
        doc_version=doc_version,
        bucket=bucket,
        changed_keys=changed_keys,
        old_prefix=old_prefix,
        is_dev=is_devel)


class PublishDocsOptions(Options):

    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of flocker from which the documetnation was built."],
        ["doc-version", None, None,
         "The version to publush the documentation as.\n"
         "This will differ from \"flocker-version\" for staging uploads and "
         "documentation releases."],
        ["bucket", None,
         b'clusterhq-staging-docs',
         "The s3 bucket to upload to."]
    ]

    def parseArgs(self):
        if self['doc-version'] is None:
            self['doc-version'] = get_doc_version(self['flocker-version'])


def publish_docs_main(args, base_path, top_level):
    """
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = PublishDocsOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    if not (is_release(options['doc-version'])
            or is_weekly_release(options['doc-version'])):
        sys.stderr.write("%s: Can't publish non-release.")
        raise SystemExit(1)

    sync_perform(
        dispatcher=ComposedDispatcher([boto_dispatcher, base_dispatcher]),
        effect=publish_docs(
            flocker_version=options['flocker-version'],
            doc_version=options['doc-version'],
            bucket_name=options['bucket']))
