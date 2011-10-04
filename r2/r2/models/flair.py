# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is Reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
#
# All portions of the code written by CondeNet are Copyright (c) 2006-2010
# CondeNet, Inc. All Rights Reserved.
################################################################################

import uuid

from pylons import g

from r2.lib.db.operators import asc, desc
from r2.lib.db.thing import Relation, Thing
from r2.lib.db import tdb_cassandra
from r2.lib.db.userrel import UserRel
from r2.lib.memoize import memoize
from r2.lib.utils import to36
from account import Account
from subreddit import Subreddit

class Flair(Relation(Subreddit, Account)):
    @classmethod
    def store(cls, sr, account, text = None, css_class = None):
        flair = cls(sr, account, 'flair', text = text, css_class = css_class)
        flair._commit()

        setattr(account, 'flair_%s_text' % sr._id, text)
        setattr(account, 'flair_%s_css_class' % sr._id, css_class)
        account._commit()

    @classmethod
    @memoize('flair.all_flair_by_sr')
    def all_flair_by_sr_cache(cls, sr_id):
        q = cls._query(cls.c._thing1_id == sr_id)
        return [t._id for t in q]

    @classmethod
    def all_flair_by_sr(cls, sr_id, _update=False):
        relids = cls.all_flair_by_sr_cache(sr_id, _update=_update)
        return cls._byID(relids).itervalues()

    @classmethod
    def flair_id_query(cls, sr, limit, after, reverse=False):
        extra_rules = [
            cls.c._thing1_id == sr._id,
            cls.c._name == 'flair',
          ]
        if after:
            if reverse:
                extra_rules.append(cls.c._thing2_id < after._id)
            else:
                extra_rules.append(cls.c._thing2_id > after._id)
        sort = (desc if reverse else asc)('_thing2_id')
        return cls._query(*extra_rules, sort=sort, limit=limit)

Subreddit.__bases__ += (UserRel('flair', Flair,
                                disable_ids_fn = True,
                                disable_reverse_ids_fn = True),)


class FlairTemplate(tdb_cassandra.Thing):
    """A template for some flair."""
    _defaults = dict(text='',
                     css_class='',
                     text_editable=False,
                    )

    _bool_props = ('text_editable',)

    _use_db = True
    _use_new_ring = True

    @classmethod
    def _new(cls, text='', css_class='', text_editable=False):
        if text is None:
            text = ''
        if css_class is None:
            css_class = ''
        ft = cls(text=text, css_class=css_class, text_editable=text_editable)
        ft._commit()
        return ft

    def _commit(self, *a, **kw):
        # Make sure an _id is always assigned before committing.
        if not self._id:
            self._id = str(uuid.uuid1())
        return tdb_cassandra.Thing._commit(self, *a, **kw)

    def covers(self, other_template):
        """Returns true if other_template is a subset of this one.

        The value for other_template may be another FlairTemplate, or a tuple
        of (text, css_class). The latter case is treated like a FlairTemplate
        that doesn't permit editable text.

        For example, if self permits editable text, then this method will return
        True as long as just the css_classes match. On the other hand, if self
        doesn't permit editable text but other_template does, this method will
        return False.
        """
        if isinstance(other_template, FlairTemplate):
            text_editable = other_template.text_editable
            text, css_class = other_template.text, other_template.css_class
        else:
            text_editable = False
            text, css_class = other_template

        if self.css_class != css_class:
            return False
        return self.text_editable or (not text_editable and self.text == text)


class FlairTemplateBySubredditIndex(tdb_cassandra.Thing):
    """A list of FlairTemplate IDs for a subreddit.

    The FlairTemplate references are stored as an arbitrary number of attrs.
    The lexicographical ordering of these attr names gives the ordering for
    flair templates within the subreddit.
    """

    _int_props = ('sr_id',)
    _use_db = True
    _use_new_ring = True

    _key_prefix = 'ft_'

    @classmethod
    def _new(cls, sr_id):
        idx = cls(_id=to36(sr_id), sr_id=sr_id)
        idx._commit()
        return idx

    @classmethod
    def _get_or_create_template(cls, sr_id, text, css_class, text_editable):
        try:
            idx = cls._byID(to36(sr_id))
        except tdb_cassandra.NotFound:
            idx = cls._new(sr_id)

        existing_ft_ids = list(idx)

    @classmethod
    def create_template(cls, sr_id, text='', css_class='', text_editable=False):
        ft = FlairTemplate._new(text=text, css_class=css_class,
                                text_editable=text_editable)
        try:
            idx = cls._byID(to36(sr_id))
        except tdb_cassandra.NotFound:
            idx = cls._new(sr_id)
        idx.insert(ft._id)
        return ft

    @classmethod
    def get_template_ids(cls, sr_id):
        try:
            return list(cls._byID(to36(sr_id)))
        except tdb_cassandra.NotFound:
            return []

    def _index_keys(self):
        keys = set(self._dirties.iterkeys())
        keys |= frozenset(self._orig.iterkeys())
        keys -= self._deletes
        return [k for k in keys if k.startswith(self._key_prefix)]

    @classmethod
    def _make_index_key(cls, position):
        return '%s%08d' % (cls._key_prefix, position)

    def __iter__(self):
        return (getattr(self, key) for key in sorted(self._index_keys()))

    def insert(self, ft_id, position=None):
        """Insert template reference into index at position.

        A position value of None means to simply append.
        """
        keys = self._index_keys()
        if position is None:
            position = len(keys)
        if position < 0 or position > len(keys):
            raise IndexError(position)

        # Move items after position to the right by one.
        for i in xrange(len(keys), position, -1):
            setattr(self, self._make_index_key(i), getattr(self, keys[i - 1]))

        # Assign to position and commit.
        setattr(self, self._make_index_key(position), ft_id)
        self._commit()
