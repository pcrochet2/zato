# -*- coding: utf-8 -*-

"""
Copyright (C) 2017, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
from bisect import bisect_left
from copy import deepcopy
from logging import getLogger
from socket import error as SocketError
from traceback import format_exc

# gevent
from gevent import sleep
from gevent.lock import RLock

# sortedcontainers
from sortedcontainers import SortedList as _SortedList

# Zato
from zato.common import PUBSUB
from zato.common.pubsub import PubSubMessage
from zato.common.util import spawn_greenlet
from zato.common.util.time_ import datetime_from_ms
from zato.server.pubsub import PubSub

# For pyflakes
PubSub = PubSub

# ################################################################################################################################

logger = getLogger('zato_pubsub')
logger_zato = getLogger('zato')

# ################################################################################################################################

_hook_action = PUBSUB.HOOK_ACTION

# ################################################################################################################################

class SortedList(_SortedList):
    """ A custom subclass that knows how to remove pubsub messages from SortedList instances.
    """
    def remove_pubsub_msg(self, msg):
        """ Removes a pubsub message from a SortedList instance - we cannot use the regular .remove method
        because it may triggger __cmp__ per https://github.com/grantjenks/sorted_containers/issues/81.
        """
        pos = bisect_left(self._maxes, msg)

        if pos == len(self._maxes):
            raise ValueError('{0!r} not in list'.format(msg))

        for _list_idx, _list_msg in enumerate(self._lists[pos]):
            if msg.pub_msg_id == _list_msg.pub_msg_id:
                idx = _list_idx
                break
        else:
            raise ValueError('{0!r} not in list'.format(msg))

        self._delete(pos, idx)

# ################################################################################################################################

class PubSubTask(object):
    """ A background task responsible for delivery of pub/sub messages each pub/sub endpoint may possibly subscribe to.
    """
    def __init__(self, sql_conn_func):
        self.sql_conn_func = sql_conn_func
        self.lock = RLock()

# ################################################################################################################################

class DeliveryTask(object):
    """ Runs a greenlet responsible for delivery of messages for a given sub_key.
    """
    def __init__(self, pubsub, sub_key, delivery_lock, delivery_list, deliver_pubsub_msg_cb, confirm_pubsub_msg_delivered_cb,
            sub_config):
        self.keep_running = True
        self.pubsub = pubsub
        self.sub_key = sub_key
        self.delivery_lock = delivery_lock
        self.delivery_list = delivery_list
        self.deliver_pubsub_msg_cb = deliver_pubsub_msg_cb
        self.confirm_pubsub_msg_delivered_cb = confirm_pubsub_msg_delivered_cb
        self.sub_config = sub_config
        self.wait_sock_err = self.sub_config.wait_sock_err
        self.wait_non_sock_err = self.sub_config.wait_non_sock_err

        # If self.wrap_in_list is True, messages will be always wrapped in a list,
        # even if there is only one message to send. Note that self.wrap_in_list will be False
        # only if both batch_size is 1 and wrap_one_msg_in_list is True.
        if self.sub_config.delivery_batch_size == 1:
            if self.sub_config.wrap_one_msg_in_list:
                self.wrap_in_list = True
            else:
                self.wrap_in_list = False

        # With batch_size > 1, we always send a list, no matter what.
        else:
            self.wrap_in_list = True

        spawn_greenlet(self.run)

    def _run_delivery(self, _run_deliv_status=PUBSUB.RUN_DELIVERY_STATUS):
        """ Actually attempts to deliver messages. Each time it runs, it gets all the messages
        that are still to be delivered from self.delivery_list.
        """
        # Try to deliver a batch of messages or a single message if batch size is 1
        # and we should not wrap it in a list.
        try:

            # Deliver up to that many messages in one batch
            batch = self.delivery_list[:self.sub_config.delivery_batch_size]

            # For each message from batch we invoke a hook, if there is any, which will decide
            # whether the message should be delivered, skipped in this iteration or perhaps deleted altogether
            # without even trying to deliver it. If there is no hook, none of messages will be skipped or deleted.

            to_delete = []
            to_deliver = []
            to_skip = []

            messages = {
                _hook_action.DELETE: to_delete,
                _hook_action.DELIVER: to_deliver,
                _hook_action.SKIP: to_skip,
            }

            # An optional pub/sub hook - note that we are checking it here rather than once upfront
            # because users may change it any time for a topic.
            hook = self.pubsub.get_before_delivery_hook(self.sub_key)

            # Without a hook we will always try to deliver all messages that we have in a given batch
            if not hook:
                to_deliver[:] = batch[:]
            else:
                # There is a hook so we can invoke it - it will update the 'messages' dict in place
                self.pubsub.invoke_before_delivery_hook(hook, self.sub_config.topic_id, self.sub_key, batch, messages)

                # Delete these messages, per response from hook (which must have existed)
                if to_delete:

                    # Mark as deleted in SQL
                    self.pubsub.set_to_delete(self.sub_key, to_delete)

                    # Delete from our in-RAM delivery list
                    with self.delivery_lock:
                        for msg in to_delete:
                            self.delivery_list.remove_pubsub_msg(msg)

            if to_skip:
                logger.info('Skipping messages `%s`', to_skip)

            # This is the call that actually delivers messages
            self.deliver_pubsub_msg_cb(self.sub_key, to_deliver if self.wrap_in_list else to_deliver[0])

        except Exception as e:
            # Do not attempt to deliver any other message, only increment delivery_count for each message
            # from that batch and return. Our parent will sleep for a small amount of time and then re-run us,
            # thanks to which the next time we run we will again iterate over all the messages
            # currently queued up, including the ones that we were not able to deliver in current iteration.

            exc = format_exc()
            logger.warn('Could not deliver pub/sub messages, e:`%s`', exc)
            logger_zato.warn('Could not deliver pub/sub messages, e:`%s`', exc)

            # ZZZ:
            #increment delivery_count for each message from to_deliver here

            return _run_deliv_status.SOCKET_ERROR if isinstance(e, SocketError) else _run_deliv_status.OTHER_ERROR

        else:
            # On successful delivery, remove these messages from SQL and our own delivery_list
            try:
                # All message IDs that we have delivered
                delivered_msg_id_list = [msg.pub_msg_id for msg in to_deliver]

                with self.delivery_lock:
                    self.confirm_pubsub_msg_delivered_cb(self.sub_key, delivered_msg_id_list)
            except Exception, e:
                logger.warn('Could not update delivery status for message(s):`%s`, e:`%s`', to_deliver, format_exc(e))
                return _run_deliv_status.SOCKET_ERROR
            else:
                with self.delivery_lock:
                    for msg in to_deliver:
                        self.delivery_list.remove_pubsub_msg(msg)

                # Status of messages is updated in both SQL and RAM so we can now log success
                logger.info('Successfully delivered message(s) %s', delivered_msg_id_list)

                # Indicates that we have successfully delivered all messages currently queued up
                # and our delivery list is currently empty.
                return _run_deliv_status.NO_MSG

    def run(self, no_msg_sleep_time=1, _run_deliv_status=PUBSUB.RUN_DELIVERY_STATUS):
        logger.info('Starting delivery task for sub_key:`%s`', self.sub_key)
        try:
            while self.keep_running:
                if not self.delivery_list:
                    sleep(no_msg_sleep_time) # No need to wake up too often if there is not much to do
                else:

                    # Get the list of all messaged IDs for which delivery was successful,
                    # indicating whether all currently lined up messages have been
                    # successfully delivered.
                    result = self._run_delivery()

                    # On success, sleep for a moment because we have just run out of all messages.
                    if result == _run_deliv_status.NO_MSG:
                        sleep(no_msg_sleep_time)

                    # Otherwise, sleep for a longer time because our endpoint must have returned an error.
                    # After this sleep, self._run_delivery will again attempt to deliver all messages
                    # we queued up. Note that we are the only delivery task for this sub_key  so when we sleep here
                    # for a moment, we do not block other deliveries.
                    else:
                        sleep_time = self.wait_sock_err if result == _run_deliv_status.SOCKET_ERROR else self.wait_non_sock_err
                        msg = 'Sleeping for {}s after `{}` in sub_key:`{}`'.format(sleep_time, result, self.sub_key)
                        logger.warn(msg)
                        logger_zato.warn(msg)
                        sleep(sleep_time)

        except Exception, e:
            error_msg = 'Exception in delivery task for sub_key:`%s`, e:`%s`'
            e_formatted = format_exc(e)
            logger.warn(error_msg, self.sub_key, e_formatted)
            logger_zato.warn(error_msg, self.sub_key, e_formatted)

    def stop(self):
        if self.keep_running:
            logger.info('Stopping delivery task for sub_key:`%s`', self.sub_key)
            self.keep_running = False

    def clear(self):
        gd, non_gd = self.get_queue_depth()
        logger.info('Removing in-RAM messages for sub_key:`%s` (GD:%d, non-GD:%d)', self.sub_key, gd, non_gd)
        self.delivery_list.clear()

    def get_queue_depth(self):
        """ Returns the number of GD and non-GD messages in delivery list.
        """
        gd = 0
        non_gd = 0

        for msg in self.delivery_list:
            if msg.has_gd:
                gd += 1
            else:
                non_gd += 1

        return gd, non_gd

    def get_gd_queue_depth(self):
        return self.get_queue_depth()[0]

    def get_non_gd_queue_depth(self):
        return self.get_queue_depth()[1]

# ################################################################################################################################

class Message(PubSubMessage):
    """ Wrapper for messages adding __cmp__ which uses a custom comparison protocol,
    by priority, then ext_pub_time, then pub_time.
    """
    def __init__(self):
        super(Message, self).__init__()
        self.sub_key = None
        self.pub_msg_id = None
        self.pub_correl_id = None
        self.in_reply_to = None
        self.ext_client_id = None
        self.group_id = None
        self.position_in_group = None
        self.pub_time = None
        self.ext_pub_time = None
        self.data = None
        self.mime_type = None
        self.priority = None
        self.expiration = None
        self.expiration_time = None
        self.has_gd = None

        self.pub_time_iso = None
        self.ext_pub_time_iso = None
        self.expiration_time_iso = None

# ################################################################################################################################

    def __cmp__(self, other, max_pri=9):
        return cmp(
            (max_pri - self.priority, self.ext_pub_time, self.pub_time),
            (max_pri - other.priority, other.ext_pub_time, other.pub_time)
        )

# ################################################################################################################################

    def __repr__(self):
        return '<Msg sk:{} id:{} ext:{} exp:{} gd:{}>'.format(
            self.sub_key, self.pub_msg_id, self.ext_client_id, datetime_from_ms(self.expiration_time), self.has_gd)

# ################################################################################################################################

    def add_iso_times(self):
        """ Sets additional attributes for datetime in ISO-8601.
        """
        self.pub_time_iso = datetime_from_ms(self.pub_time)

        if self.ext_pub_time:
            self.ext_pub_time_iso = datetime_from_ms(self.ext_pub_time)

        if self.expiration_time:
            self.expiration_time_iso = datetime_from_ms(self.expiration_time)

# ################################################################################################################################

class GDMessage(Message):
    """ A guaranteed delivery message initialized from SQL data.
    """
    def __init__(self, sub_key, msg):
        super(GDMessage, self).__init__()
        self.sub_key = sub_key
        self.pub_msg_id = msg.pub_msg_id
        self.pub_correl_id = msg.pub_correl_id
        self.in_reply_to = msg.in_reply_to
        self.ext_client_id = msg.ext_client_id
        self.group_id = msg.group_id
        self.position_in_group = msg.position_in_group
        self.pub_time = msg.pub_time
        self.ext_pub_time = msg.ext_pub_time
        self.data = msg.data
        self.mime_type = msg.mime_type
        self.priority = msg.priority
        self.expiration = msg.expiration
        self.expiration_time = msg.expiration_time
        self.has_gd = msg.has_gd

        # Add times in ISO-8601 for external subscribers
        self.add_iso_times()

# ################################################################################################################################

class NonGDMessage(Message):
    """ A non-guaranteed delivery message initialized from a Python dict.
    """
    def __init__(self, sub_key, msg):
        super(NonGDMessage, self).__init__()
        self.sub_key = sub_key
        self.pub_msg_id = msg['pub_msg_id']
        self.pub_correl_id = msg['pub_correl_id']
        self.in_reply_to = msg['in_reply_to']
        self.ext_client_id = msg['ext_client_id']
        self.group_id = msg['group_id']
        self.position_in_group = msg['position_in_group']
        self.pub_time = msg['pub_time']
        self.ext_pub_time = msg['ext_pub_time']
        self.data = msg['data']
        self.mime_type = msg['mime_type']
        self.priority = msg['priority']
        self.expiration = msg['expiration']
        self.expiration_time = msg['expiration_time']
        self.has_gd = msg['has_gd']

        # Add times in ISO-8601 for external subscribers
        self.add_iso_times()

# ################################################################################################################################

class PubSubTool(object):
    """ A utility object for pub/sub-related tasks.
    """
    def __init__(self, pubsub, parent, endpoint_type, deliver_pubsub_msg=None):
        self.pubsub = pubsub # type: PubSub
        self.parent = parent # This is our parent, e.g. an individual WebSocket on whose behalf we execute
        self.endpoint_type = endpoint_type

        # WSX connections will have their own callback but other connections use the default one
        self.deliver_pubsub_msg = deliver_pubsub_msg or self.pubsub.deliver_pubsub_msg

        # A broad lock for generic pub/sub matters
        self.lock = RLock()

        # Each sub_key will get its own lock for operations related to that key only
        self.sub_key_locks = {}

        # How many messages to send in a single delivery group,
        # may be set individually for each subscription, defaults to 1
        self.batch_size = {}

        # Which sub_keys this pubsub_tool handles
        self.sub_keys = set()

        # A sorted list of message references for each sub_key
        self.delivery_lists = {}

        # A pub/sub delivery task for each sub_key
        self.delivery_tasks = {}

        # For each sub key, when was an SQL query last executed
        # that SELECT-ed latest messages for that sub_key.
        self.last_sql_run = {}

        # Register with this server's pubsub
        self.register_pubsub_tool()

# ################################################################################################################################

    def register_pubsub_tool(self):
        """ Registers ourselves with this server's pubsub to let the other control when we should shut down
        our delivery tasks for each sub_key.
        """
        self.pubsub.register_pubsub_tool(self)

# ################################################################################################################################

    def add_sub_key_no_lock(self, sub_key):
        """ Adds metadata about a given sub_key - must be called with self.lock held.
        """
        # Already seen it - can be ignored
        if sub_key in self.sub_keys:
            return

        self.sub_keys.add(sub_key)
        self.batch_size[sub_key] = 1
        self.last_sql_run[sub_key] = None

        delivery_list = SortedList()
        delivery_lock = RLock()

        self.delivery_lists[sub_key] = delivery_list
        self.delivery_tasks[sub_key] = DeliveryTask(
            self.pubsub, sub_key, delivery_lock, delivery_list, self.deliver_pubsub_msg,
            self.confirm_pubsub_msg_delivered, self.pubsub.get_subscription_by_sub_key(sub_key).config)

        self.sub_key_locks[sub_key] = delivery_lock

# ################################################################################################################################

    def add_sub_key(self, sub_key):
        """ Same as self.add_sub_key_no_lock but holds self.lock.
        """
        with self.lock:
            self.add_sub_key_no_lock(sub_key)
            self.pubsub.set_pubsub_tool_for_sub_key(sub_key, self)

# ################################################################################################################################

    def remove_sub_key(self, sub_key):
        with self.lock:
            try:
                self.sub_keys.remove(sub_key)
                del self.batch_size[sub_key]
                del self.last_sql_run[sub_key]
                del self.sub_key_locks[sub_key]

                del self.delivery_lists[sub_key]
                self.delivery_tasks[sub_key].stop()
                del self.delivery_tasks[sub_key]

            except Exception, e:
                logger.warn('Exception during sub_key removal `%s`, e:`%s`', sub_key, format_exc(e))

# ################################################################################################################################

    def remove_all_sub_keys(self):
        sub_keys = deepcopy(self.sub_keys)
        for sub_key in sub_keys:
            self.remove_sub_key(sub_key)

# ################################################################################################################################

    def _add_non_gd_messages_by_sub_key(self, sub_key, messages):
        """ Low-level implementation of add_non_gd_messages_by_sub_key,
        must be called with a lock for input sub_key.
        """
        for msg in messages:
            self.delivery_lists[sub_key].add(NonGDMessage(sub_key, msg))

# ################################################################################################################################

    def add_non_gd_messages_by_sub_key(self, sub_key, messages):
        """ Adds to local delivery queue all non-GD messages from input.
        """
        with self.sub_key_locks[sub_key]:
            self._add_non_gd_messages_by_sub_key(sub_key, messages)

# ################################################################################################################################

    def handle_new_messages(self, cid, has_gd, sub_key_list, non_gd_msg_list):
        """ A callback invoked when there is at least one new message to be handled for input sub_keys.
        If has_gd is True, it means that at least one GD message available. If non_gd_msg_list is not empty,
        it is a list of non-GD message for sub_keys.
        """
        if not has_gd:
            if not non_gd_msg_list:
                raise ValueError('No messages received ({}) for cid:`{}`, has_gd:`{}` and sub_key_list:`{}`'.format(
                    non_gd_msg_list, cid, has_gd, sub_key_list))
        # Iterate over all input sub keys and carry out all operations while holding a lock for each sub_key
        for sub_key in sub_key_list:
            with self.sub_key_locks[sub_key]:

                # Fetch all GD messages, if there are any at all
                if has_gd:
                    self._fetch_gd_messages_by_sub_key(sub_key)

                # Accept all input non-GD messages
                if non_gd_msg_list:
                    self._add_non_gd_messages_by_sub_key(sub_key, non_gd_msg_list)

# ################################################################################################################################

    def _fetch_gd_messages_by_sub_key(self, sub_key, session=None):
        """ Low-level implementation of fetch_gd_messages_by_sub_key,
        must be called with a lock for input sub_key.
        """
        for msg in self.pubsub.get_sql_messages_by_sub_key(sub_key, self.last_sql_run[sub_key], session):
            self.delivery_lists[sub_key].add(GDMessage(sub_key, msg))

# ################################################################################################################################

    def fetch_gd_messages_by_sub_key(self, sub_key, session=None):
        """ Fetches GD messages from SQL for sub_key given on input and adds them to local queue of messages to deliver.
        """
        with self.sub_key_locks[sub_key]:
            self._fetch_gd_messages_by_sub_key(sub_key, session)

# ################################################################################################################################

    def confirm_pubsub_msg_delivered(self, sub_key, delivered_list):
        self.pubsub.confirm_pubsub_msg_delivered(sub_key, delivered_list)

# ################################################################################################################################

    def get_queue_depth(self, sub_key):
        """ Returns the number of GD and non-GD messages queued up for input sub_key.
        """
        with self.sub_key_locks[sub_key]:
            return self.delivery_tasks[sub_key].get_queue_depth()

# ################################################################################################################################

    def handles_sub_key(self, sub_key):
        with self.lock:
            return sub_key in self.sub_keys

# ################################################################################################################################

    def get_delivery_task(self, sub_key):
        with self.lock:
            return self.delivery_tasks[sub_key]

# ################################################################################################################################
