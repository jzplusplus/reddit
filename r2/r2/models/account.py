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
from r2.lib.db.thing     import Thing, Relation, NotFound
from r2.lib.db.operators import lower
from r2.lib.db.userrel   import UserRel
from r2.lib.memoize      import memoize
from r2.lib.utils        import modhash, valid_hash, randstr, timefromnow
from r2.lib.utils        import UrlParser, set_last_visit, last_visit
from r2.lib.utils        import constant_time_compare
from r2.lib.cache        import sgm
from r2.lib import filters
from r2.lib.log import log_text

from pylons import g
import time, sha
from copy import copy
from datetime import datetime, timedelta
import bcrypt

class AccountExists(Exception): pass

class Account(Thing):
    _data_int_props = Thing._data_int_props + ('link_karma', 'comment_karma',
                                               'report_made', 'report_correct',
                                               'report_ignored', 'spammer',
                                               'reported', 'gold_creddits', )
    _int_prop_suffix = '_karma'
    _essentials = ('name', )
    _defaults = dict(pref_numsites = 25,
                     pref_frame = False,
                     pref_frame_commentspanel = False,
                     pref_newwindow = False,
                     pref_clickgadget = 5,
                     pref_public_votes = False,
                     pref_hide_from_robots = False,
                     pref_research = False,
                     pref_hide_ups = False,
                     pref_hide_downs = False,
                     pref_min_link_score = -4,
                     pref_min_comment_score = -4,
                     pref_num_comments = g.num_comments,
                     pref_lang = g.lang,
                     pref_content_langs = (g.lang,),
                     pref_over_18 = False,
                     pref_compress = False,
                     pref_organic = True,
                     pref_no_profanity = True,
                     pref_label_nsfw = True,
                     pref_show_stylesheets = True,
                     pref_show_flair = True,
                     pref_mark_messages_read = True,
                     pref_threaded_messages = True,
                     pref_collapse_read_messages = False,
                     pref_private_feeds = True,
                     pref_local_js = False,
                     pref_show_adbox = True,
                     pref_show_sponsors = True, # sponsored links
                     pref_show_sponsorships = True,
                     pref_highlight_new_comments = True,
                     mobile_compress = False,
                     mobile_thumbnail = True,
                     trusted_sponsor = False,
                     reported = 0,
                     report_made = 0,
                     report_correct = 0,
                     report_ignored = 0,
                     spammer = 0,
                     sort_options = {},
                     has_subscribed = False,
                     pref_media = 'subreddit',
                     share = {},
                     wiki_override = None,
                     email = "",
                     email_verified = False,
                     ignorereports = False,
                     pref_show_promote = None,
                     gold = False,
                     gold_charter = False,
                     gold_creddits = 0,
                     gold_creddit_escrow = 0,
                     )

    def karma(self, kind, sr = None):
        suffix = '_' + kind + '_karma'

        #if no sr, return the sum
        if sr is None:
            total = 0
            for k, v in self._t.iteritems():
                if k.endswith(suffix):
                    total += v
            return total
        else:
            try:
                return getattr(self, sr.name + suffix)
            except AttributeError:
                #if positive karma elsewhere, you get min_up_karma
                if self.karma(kind) > 0:
                    return g.MIN_UP_KARMA
                else:
                    return 0

    def incr_karma(self, kind, sr, amt):
        if sr.name.startswith('_'):
            g.log.info("Ignoring karma increase for subreddit %r" % (sr.name,))
            return

        prop = '%s_%s_karma' % (sr.name, kind)
        if hasattr(self, prop):
            return self._incr(prop, amt)
        else:
            default_val = self.karma(kind, sr)
            setattr(self, prop, default_val + amt)
            self._commit()

    @property
    def link_karma(self):
        return self.karma('link')

    @property
    def comment_karma(self):
        return self.karma('comment')

    @property
    def safe_karma(self):
        karma = self.link_karma
        return max(karma, 1) if karma > -1000 else karma

    def can_wiki(self):
        if self.wiki_override is not None:
            return self.wiki_override
        else:
            return (self.link_karma >= g.WIKI_KARMA and
                    self.comment_karma >= g.WIKI_KARMA)

    def jury_betatester(self):
        if g.cache.get("jury-killswitch"):
            return False
        else:
            return True

    def all_karmas(self):
        """returns a list of tuples in the form (name, link_karma,
        comment_karma)"""
        link_suffix = '_link_karma'
        comment_suffix = '_comment_karma'
        karmas = []
        sr_names = set()
        for k in self._t.keys():
            if k.endswith(link_suffix):
                sr_names.add(k[:-len(link_suffix)])
            elif k.endswith(comment_suffix):
                sr_names.add(k[:-len(comment_suffix)])
        for sr_name in sr_names:
            karmas.append((sr_name,
                           self._t.get(sr_name + link_suffix, 0),
                           self._t.get(sr_name + comment_suffix, 0)))

        karmas.sort(key = lambda x: abs(x[1] + x[2]), reverse=True)

        karmas.insert(0, ('total',
                          self.karma('link'),
                          self.karma('comment')))

        karmas.append(('old',
                       self._t.get('link_karma', 0),
                       self._t.get('comment_karma', 0)))

        return karmas

    def update_last_visit(self, current_time):
        from admintools import apply_updates

        apply_updates(self)

        #prev_visit = getattr(self, 'last_visit', None)
        prev_visit = last_visit(self)

        if prev_visit and current_time - prev_visit < timedelta(1):
            return

        g.log.debug ("Updating last visit for %s from %s to %s" %
                    (self.name, prev_visit, current_time))
        set_last_visit(self)

    def make_cookie(self, timestr = None, admin = False):
        if not self._loaded:
            self._load()
        timestr = timestr or time.strftime('%Y-%m-%dT%H:%M:%S')
        id_time = str(self._id) + ',' + timestr
        to_hash = ','.join((id_time, self.password, g.SECRET))
        if admin:
            to_hash += 'admin'
        return id_time + ',' + sha.new(to_hash).hexdigest()

    def needs_captcha(self):
        return self.link_karma < 1 and not g.disable_captcha

    def modhash(self, rand=None, test=False):
        return modhash(self, rand = rand, test = test)
    
    def valid_hash(self, hash):
        return valid_hash(self, hash)

    @classmethod
    @memoize('account._by_name')
    def _by_name_cache(cls, name, allow_deleted = False):
        #relower name here, just in case
        deleted = (True, False) if allow_deleted else False
        q = cls._query(lower(Account.c.name) == name.lower(),
                       Account.c._spam == (True, False),
                       Account.c._deleted == deleted)

        q._limit = 1
        l = list(q)
        if l:
            return l[0]._id

    @classmethod
    def _by_name(cls, name, allow_deleted = False, _update = False):
        #lower name here so there is only one cache
        uid = cls._by_name_cache(name.lower(), allow_deleted, _update = _update)
        if uid:
            return cls._byID(uid, True)
        else:
            raise NotFound, 'Account %s' % name

    # Admins only, since it's not memoized
    @classmethod
    def _by_name_multiple(cls, name):
        q = cls._query(lower(Account.c.name) == name.lower(),
                       Account.c._spam == (True, False),
                       Account.c._deleted == (True, False))
        return list(q)

    @property
    def friends(self):
        return self.friend_ids()

    @property
    def enemies(self):
        return self.enemy_ids()

    # Used on the goldmember version of /prefs/friends
    @memoize('account.friend_rels')
    def friend_rels_cache(self):
        q = Friend._query(Friend.c._thing1_id == self._id,
                          Friend.c._name == 'friend')
        return list(f._id for f in q)

    def friend_rels(self, _update = False):
        rel_ids = self.friend_rels_cache(_update=_update)
        try:
            rels = Friend._byID_rel(rel_ids, return_dict=False,
                                    eager_load = True, data = True,
                                    thing_data = True)
            rels = list(rels)
        except NotFound:
            if _update:
                raise
            else:
                log_text("friend-rels-bandaid 1",
                         "Had to recalc friend_rels (1) for %s" % self.name,
                         "warning")
                return self.friend_rels(_update=True)

        if not _update:
            sorted_1 = sorted([r._thing2_id for r in rels])
            sorted_2 = sorted(list(self.friends))
            if sorted_1 != sorted_2:
                g.log.error("FR1: %r" % sorted_1)
                g.log.error("FR2: %r" % sorted_2)
                log_text("friend-rels-bandaid 2",
                         "Had to recalc friend_rels (2) for %s" % self.name,
                         "warning")
                self.friend_ids(_update=True)
                return self.friend_rels(_update=True)
        return dict((r._thing2_id, r) for r in rels)

    def add_friend_note(self, friend, note):
        rels = self.friend_rels()
        rel = rels[friend._id]
        rel.note = note
        rel._commit()

    def delete(self):
        self._deleted = True
        self._commit()

        #update caches
        Account._by_name(self.name, allow_deleted = True, _update = True)
        #we need to catch an exception here since it will have been
        #recently deleted
        try:
            Account._by_name(self.name, _update = True)
        except NotFound:
            pass
        
        #remove from friends lists
        q = Friend._query(Friend.c._thing2_id == self._id,
                          Friend.c._name == 'friend',
                          eager_load = True)
        for f in q:
            f._thing1.remove_friend(f._thing2)

        q = Friend._query(Friend.c._thing2_id == self._id,
                          Friend.c._name == 'enemy',
                          eager_load=True)
        for f in q:
            f._thing1.remove_enemy(f._thing2)

    @property
    def subreddits(self):
        from subreddit import Subreddit
        return Subreddit.user_subreddits(self)

    def recent_share_emails(self):
        return self.share.get('recent', set([]))

    def add_share_emails(self, emails):
        if not emails:
            return
        
        if not isinstance(emails, set):
            emails = set(emails)

        self.share.setdefault('emails', {})
        share = self.share.copy()

        share_emails = share['emails']
        for e in emails:
            share_emails[e] = share_emails.get(e, 0) +1

        share['recent'] = emails

        self.share = share

    def set_cup(self, cup_info):
        from r2.lib.template_helpers import static

        if cup_info is None:
            return

        if cup_info.get("expiration", None) is None:
            return

        cup_info.setdefault("label_template",
          "%(user)s recently won a trophy! click here to see it.")

        cup_info.setdefault("img_url", static('award.png'))

        existing_info = self.cup_info()

        if (existing_info and
            existing_info["expiration"] > cup_info["expiration"]):
            # The existing award has a later expiration,
            # so it trumps the new one as far as cups go
            return

        td = cup_info["expiration"] - timefromnow("0 seconds")

        cache_lifetime = td.seconds

        if cache_lifetime <= 0:
            g.log.error("Adding a cup that's already expired?")
        else:
            g.hardcache.set("cup_info-%d" % self._id, cup_info, cache_lifetime)

    def remove_cup(self):
        g.hardcache.delete("cup_info-%d" % self._id)

    def cup_info(self):
        return g.hardcache.get("cup_info-%d" % self._id)

    def special_distinguish(self):
        if self._t.get("special_distinguish_name"):
            return dict((k, self._t.get("special_distinguish_"+k, None))
                        for k in ("name", "kind", "symbol", "cssclass", "label", "link"))
        else:
            return None

    def quota_key(self, kind):
        return "user_%s_quotas-%s" % (kind, self.name)

    def clog_quota(self, kind, item):
        key = self.quota_key(kind)
        fnames = g.hardcache.get(key, [])
        fnames.append(item._fullname)
        g.hardcache.set(key, fnames, 86400 * 30)

    def quota_baskets(self, kind):
        from r2.models.admintools import filter_quotas
        key = self.quota_key(kind)
        fnames = g.hardcache.get(key)

        if not fnames:
            return None

        unfiltered = Thing._by_fullname(fnames, data=True, return_dict=False)

        baskets, new_quotas = filter_quotas(unfiltered)

        if new_quotas is None:
            pass
        elif new_quotas == []:
            g.hardcache.delete(key)
        else:
            g.hardcache.set(key, new_quotas, 86400 * 30)

        return baskets

    # Needs to take the *canonicalized* version of each email
    # When true, returns the reason
    @classmethod
    def which_emails_are_banned(cls, canons):
        banned = g.hardcache.get_multi(canons, prefix="email_banned-")

        # Filter out all the ones that are simply banned by address.
        # Of the remaining ones, create a dictionary like:
        # d["abc.def.com"] = [ "bob@abc.def.com", "sue@abc.def.com" ]
        rv = {}
        canons_by_domain = {}
        for canon in canons:
            if banned.get(canon, False):
                rv[canon] = "address"
                continue
            rv[canon] = None

            at_sign = canon.find("@")
            domain = canon[at_sign+1:]
            canons_by_domain.setdefault(domain, [])
            canons_by_domain[domain].append(canon)

        # Now, build a list of subdomains to check for ban status; for
        # abc@foo.bar.com, we need to check foo.bar.com and bar.com
        canons_by_subdomain = {}
        for domain, canons in canons_by_domain.iteritems():
            parts = domain.rstrip(".").split(".")
            while len(parts) >= 2:
                whole = ".".join(parts)
                canons_by_subdomain.setdefault(whole, [])
                canons_by_subdomain[whole].extend(canons)
                parts.pop(0)

        banned_subdomains = {}
        sub_dict = g.hardcache.get_multi(canons_by_subdomain.keys(),
                                         prefix="domain-")
        for subdomain, d in sub_dict.iteritems():
            if d and d.get("no_email", None):
                for canon in canons_by_subdomain[subdomain]:
                    rv[canon] = "domain"

        return rv

    def has_banned_email(self):
        canon = self.canonical_email()
        which = self.which_emails_are_banned((canon,))
        return which.get(canon, None)

    def canonical_email(self):
        email = str(self.email.lower())
        if email.count("@") != 1:
            return "invalid@invalid.invalid"

        localpart, domain = email.split("@")

        # a.s.d.f+something@gmail.com --> asdf@gmail.com
        localpart.replace(".", "")
        plus = localpart.find("+")
        if plus > 0:
            localpart = localpart[:plus]

        return localpart + "@" + domain

    def cromulent(self):
        """Return whether the user has validated their email address and
           passes some rudimentary 'not evil' checks."""

        if not self.email_verified:
            return False

        if self.has_banned_email():
            return False

        # Otherwise, congratulations; you're cromulent!
        return True

    def quota_limits(self, kind):
        if kind != 'link':
            raise NotImplementedError

        if self.cromulent():
            return dict(hour=3, day=10, week=50, month=150)
        else:
            return dict(hour=1,  day=3,  week=5,   month=5)

    def quota_full(self, kind):
        limits = self.quota_limits(kind)
        baskets = self.quota_baskets(kind)

        if baskets is None:
            return None

        total = 0
        filled_quota = None
        for key in ('hour', 'day', 'week', 'month'):
            total += len(baskets[key])
            if total >= limits[key]:
                filled_quota = key

        return filled_quota

    @classmethod
    def cup_info_multi(cls, ids):
        return g.hardcache.get_multi(ids, prefix="cup_info-")

    @classmethod
    def system_user(cls):
        try:
            return cls._by_name(g.system_user)
        except (NotFound, AttributeError):
            return None

    def flair_enabled_in_sr(self, sr_id):
        return getattr(self, 'flair_%d_enabled' % sr_id, True)

class FakeAccount(Account):
    _nodb = True
    pref_no_profanity = True


def valid_cookie(cookie):
    try:
        uid, timestr, hash = cookie.split(',')
        uid = int(uid)
    except:
        return (False, False)

    if g.read_only_mode:
        return (False, False)

    try:
        account = Account._byID(uid, True)
        if account._deleted:
            return (False, False)
    except NotFound:
        return (False, False)

    if constant_time_compare(cookie, account.make_cookie(timestr, admin = False)):
        return (account, False)
    elif constant_time_compare(cookie, account.make_cookie(timestr, admin = True)):
        return (account, True)
    return (False, False)

def valid_feed(name, feedhash, path):
    if name and feedhash and path:
        from r2.lib.template_helpers import add_sr
        path = add_sr(path)
        try:
            user = Account._by_name(name)
            if (user.pref_private_feeds and
                constant_time_compare(feedhash, make_feedhash(user, path))):
                return user
        except NotFound:
            pass

def make_feedhash(user, path):
    return sha.new("".join([user.name, user.password, g.FEEDSECRET])
                   ).hexdigest()

def make_feedurl(user, path, ext = "rss"):
    u = UrlParser(path)
    u.update_query(user = user.name,
                   feed = make_feedhash(user, path))
    u.set_extension(ext)
    return u.unparse()

def valid_login(name, password):
    try:
        a = Account._by_name(name)
    except NotFound:
        return False

    if not a._loaded: a._load()
    return valid_password(a, password)

def valid_password(a, password):
    # bail out early if the account or password's invalid
    if not hasattr(a, 'name') or not hasattr(a, 'password') or not password:
        return False

    # standardize on utf-8 encoding
    password = filters._force_utf8(password)

    # this is really easy if it's a sexy bcrypt password
    if a.password.startswith('$2a$'):
        expected_hash = bcrypt.hashpw(password, a.password)
        if constant_time_compare(a.password, expected_hash):
            return a
        return False

    # alright, so it's not bcrypt. how old is it?
    # if the length of the stored hash is 43 bytes, the sha-1 hash has a salt
    # otherwise it's sha-1 with no salt.
    salt = ''
    if len(a.password) == 43:
        salt = a.password[:3]
    expected_hash = passhash(a.name, password, salt)

    if not constant_time_compare(a.password, expected_hash):
        return False

    # since we got this far, it's a valid password but in an old format
    # let's upgrade it
    a.password = bcrypt_password(password)
    a._commit()
    return a

def bcrypt_password(password):
    salt = bcrypt.gensalt(log_rounds=g.bcrypt_work_factor)
    return bcrypt.hashpw(password, salt)

def passhash(username, password, salt = ''):
    if salt is True:
        salt = randstr(3)
    tohash = '%s%s %s' % (salt, username, password)
    return salt + sha.new(tohash).hexdigest()

def change_password(user, newpassword):
    user.password = bcrypt_password(newpassword)
    user._commit()
    return True

#TODO reset the cache
def register(name, password):
    try:
        a = Account._by_name(name)
        raise AccountExists
    except NotFound:
        a = Account(name = name,
                    password = bcrypt_password(password))
        # new accounts keep the profanity filter settings until opting out
        a.pref_no_profanity = True
        a._commit()

        #clear the caches
        Account._by_name(name, _update = True)
        Account._by_name(name, allow_deleted = True, _update = True)
        return a

class Friend(Relation(Account, Account)): pass

Account.__bases__ += (UserRel('friend', Friend, disable_reverse_ids_fn=True),
                      UserRel('enemy', Friend, disable_reverse_ids_fn=False))

class DeletedUser(FakeAccount):
    @property
    def name(self):
        return '[deleted]'

    @property
    def _deleted(self):
        return True

    def _fullname(self):
        raise NotImplementedError

    def _id(self):
        raise NotImplementedError

    def __setattr__(self, attr, val):
        if attr == '_deleted':
            pass
        else:
            object.__setattr__(self, attr, val)
