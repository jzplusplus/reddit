# "The contents of this file are subject to the Common Public Attribution
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
# All portions of the code written by CondeNet are Copyright (c) 2006-2008
# CondeNet, Inc. All Rights Reserved.
##############################################################################
from r2.config.databases import email_engine
from r2.lib.db.tdb_sql import make_metadata, settings
from sqlalchemy.databases.postgres import PGInet, PGBigInteger
from r2.models.thing_changes import changed, index_str, create_table
import sqlalchemy as sa
import datetime
from r2.lib.utils import Storage, timeago
from account import Account
from r2.lib.db.thing import Thing
from email.MIMEText import MIMEText
import sha
from r2.lib.memoize import memoize, clear_memo


def mail_queue(metadata):
    return sa.Table(settings.DB_APP_NAME + '_mail_queue', metadata,
                    sa.Column("uid", sa.Integer,
                              sa.Sequence('queue_id_seq'), primary_key=True),

                    # unique hash of the message to carry around
                    sa.Column("msg_hash", sa.String),
                    
                    # the id of the account who started it
                    sa.Column('account_id', PGBigInteger),

                    # the name (not email) for the from
                    sa.Column('from_name', sa.String),

                    # the "To" address of the email
                    sa.Column('to_addr', sa.String),

                    # fullname of the thing
                    sa.Column('fullname', sa.String),
                    
                    # when added to the queue
                    sa.Column('date',
                              sa.DateTime(timezone = True),
                              nullable = False),

                    # IP of original request
                    sa.Column('ip', PGInet),

                    # enum of kind of event
                    sa.Column('kind', sa.Integer),
                    
                    # any message that may have been included
                    sa.Column('body', sa.String),
                    
                    )

def sent_mail_table(metadata):
    return sa.Table(settings.DB_APP_NAME + '_sent_mail', metadata,
                    # tracking hash of the email
                    sa.Column('msg_hash', sa.String, primary_key=True),
                    
                    # the account who started it
                    sa.Column('account_id', PGBigInteger),
                    
                    # the "To" address of the email
                    sa.Column('to_addr', sa.String),

                    # IP of original request
                    sa.Column('ip', PGInet),

                    # fullname of the reference thing
                    sa.Column('fullname', sa.String),

                    # send date
                    sa.Column('date',
                              sa.DateTime(timezone = True),
                              default = sa.func.now(),
                              nullable = False),

                    # enum of kind of event
                    sa.Column('kind', sa.Integer),

                    )
                    

def opt_out(metadata):
    return sa.Table(settings.DB_APP_NAME + '_opt_out', metadata,
                    sa.Column('email', sa.String, primary_key = True),
                    # when added to the list
                    sa.Column('date',
                              sa.DateTime(timezone = True),
                              default = sa.func.now(),
                              nullable = False),
                    # why did they do it!?
                    sa.Column('msg_hash', sa.String),
                    )

class EmailHandler(object):
    def __init__(self, force = False):
        self.metadata = make_metadata(email_engine)
        self.queue_table = mail_queue(self.metadata)
        indices = [index_str(self.queue_table, "date", "date"),
                   index_str(self.queue_table, 'kind', 'kind')]
        create_table(self.queue_table, indices, force = force)

        self.opt_table = opt_out(self.metadata)
        indices = [index_str(self.opt_table, 'email', 'email')]
        create_table(self.opt_table, indices, force = force)

        self.track_table = sent_mail_table(self.metadata)
        indices = [index_str(self.track_table, 'to_addr', 'to_addr'),
                   index_str(self.track_table, 'date', 'date'),
                   index_str(self.track_table, 'ip', 'ip'),
                   index_str(self.track_table, 'kind', 'kind'),
                   index_str(self.track_table, 'fullname', 'fullname'),
                   index_str(self.track_table, 'account_id', 'account_id'),
                   index_str(self.track_table, 'msg_hash', 'msg_hash'),
                   ]
        create_table(self.track_table, indices, force = force)

    def __repr__(self):
        return "<email-handler>"

    def has_opted_out(self, email):
        o = self.opt_table
        s = sa.select([o.c.email], o.c.email == email, limit = 1)
        res = s.execute()
        return bool(res.fetchall())

    def opt_out(self, msg_hash):
        """Adds the recipient of the email to the opt-out list and returns
        that address."""
        email = self.get_recipient(msg_hash)
        if email:
            o = self.opt_table
            try:
                o.insert().execute({o.c.email: email, o.c.msg_hash: msg_hash})
                clear_memo('r2.models.mail_queue.has_opted_out', 
                           email)
                return (email, True)
            except sa.exceptions.SQLError:
                return (email, False)
        return (None, False)

    def opt_in(self, msg_hash):
        """Removes recipient of the email from the opt-out list"""
        email = self.get_recipient(msg_hash)
        if email:
            o = self.opt_table
            if self.has_opted_out(email):
                sa.delete(o, o.c.email == email).execute()
                clear_memo('r2.models.mail_queue.has_opted_out',
                           email)
                return (email, True)
            else:
                return (email, False)
        return (None, False)
        
    def get_recipient(self, msg_hash):
        t = self.track_table
        s = sa.select([t.c.to_addr], t.c.msg_hash == msg_hash).execute()
        res = s.fetchall()
        return res[0][0] if res and res[:1] else None

        
    def add_to_queue(self, user, thing, emails, from_name, date, ip,
                     kind, body = ""):
        s = self.queue_table
        hashes = []
        for email in emails:
            uid = user._id if user else 0
            tid = thing._fullname if thing else ""
            key = sha.new(str((email, from_name, uid, tid, ip, kind, body,
                               datetime.datetime.now()))).hexdigest()
            s.insert().execute({s.c.to_addr : email,
                                s.c.account_id : uid,
                                s.c.from_name : from_name,
                                s.c.fullname: tid, 
                                s.c.ip : ip,
                                s.c.kind: kind,
                                s.c.body: body,
                                s.c.date : date,
                                s.c.msg_hash : key})
            hashes.append(key)
        return hashes


    def from_queue(self, max_date, batch_limit = 50, kind = None):
        from r2.models import is_banned_IP, Account, Thing
        keep_trying = True
        min_id = None
        s = self.queue_table
        while keep_trying:
            where = [s.c.date < max_date]
            if min_id:
                where.append(s.c.uid > min_id)
            if kind:
                where.append(s.c.kind == kind)
                
            res = sa.select([s.c.to_addr, s.c.account_id,
                             s.c.from_name, s.c.fullname, s.c.body, 
                             s.c.kind, s.c.ip, s.c.date, s.c.uid,
                             s.c.msg_hash],
                            sa.and_(*where),
                            order_by = s.c.uid, limit = batch_limit).execute()
            res = res.fetchall()

            if not res: break

            # batch load user accounts
            aids = [x[1] for x in res if x[1] > 0]
            accts = Account._byID(aids, data = True,
                                  return_dict = True) if aids else {}

            # batch load things
            tids = [x[3] for x in res if x[3]]
            things = Thing._by_fullname(tids, data = True,
                                        return_dict = True) if tids else {}

            # make sure no IPs have been banned in the mean time
            ips = set(x[6] for x in res)
            ips = dict((ip, is_banned_IP(ip)) for ip in ips)

            # get the lower bound date for next iteration
            min_id = max(x[8] for x in res)

            # did we not fetch them all?
            keep_trying = (len(res) == batch_limit)
        
            for addr, acct, fname, fulln, body, kind, ip, date, uid, msg_hash \
                    in res:
                yield (accts.get(acct), things.get(fulln), addr,
                       fname, date, ip, ips[ip], kind, msg_hash, body)
                
    def clear_queue(self, max_date, kind = None):
        s = self.queue_table
        where = [s.c.date < max_date]
        if kind:
            where.append([s.c.kind == kind])
        sa.delete(s, sa.and_(*where)).execute()


class Email(object):
    handler = EmailHandler()

    Kind = ["SHARE", "FEEDBACK", "ADVERTISE", "OPTOUT", "OPTIN"]
    Kind = Storage((e, i) for i, e in enumerate(Kind))

    def __init__(self, user, thing, email, from_name, date, ip, banned_ip,
                 kind, msg_hash, body = '', subject = "", from_addr = ''):
        self.user = user
        self.thing = thing
        self.to_addr = email
        self.fr_addr = from_addr
        self._from_name = from_name
        self.date = date
        self.ip = ip
        self.banned_ip = banned_ip
        self.kind = kind
        self.sent = False
        self.body = ""
        self.subject = subject
        self.msg_hash = msg_hash

    def from_name(self):
        return ("%(name)s (%(uname)s)" if self._from_name != self.user.name
                else "%(uname)s") % \
                dict(name = self._from_name, uname = self.user.name)

    @classmethod
    def get_unsent(cls, max_date, batch_limit = 50, kind = None):
        for e in cls.handler.from_queue(max_date, batch_limit = batch_limit,
                                        kind = kind):
            yield cls(*e)

    def should_queue(self):
        return (not self.user  or not self.user._spam) and \
               (not self.thing or not self.thing._spam) and \
               not self.banned_ip and \
               (self.kind == self.Kind.OPTOUT or
                not has_opted_out(self.to_addr))

    def set_sent(self, date = None):
        if not self.sent:
            from pylons import g
            self.date = date or datetime.datetime.now(g.tz)
            t = self.handler.track_table
            t.insert().execute({t.c.account_id:
                                self.user._id if self.user else 0,
                                t.c.to_addr :   self.to_addr,
                                t.c.ip :        self.ip,
                                t.c.fullname:
                                self.thing._fullname if self.thing else "",
                                t.c.date:       self.date,
                                t.c.kind :      self.kind,
                                t.c.msg_hash :  self.msg_hash,
                                })
            self.sent = True

    def to_MIMEText(self):
        def utf8(s):
            return s.encode('utf8') if isinstance(s, unicode) else s
        fr = '"%s" <%s>' % (self._from_name, self.fr_addr) if self._from_name else self.fr_addr
        if not fr.startswith('-') and not self.to_addr.startswith('-'): # security
            msg = MIMEText(utf8(self.body))
            msg.set_charset('utf8')
            msg['To']      = utf8(self.to_addr)
            msg['From']    = utf8(fr)
            msg['Subject'] = utf8(self.subject)
            if self.user:
                msg['X-Reddit-username'] = utf8(self.user.name)
            msg['X-Reddit-ID'] = self.msg_hash
            return msg
        return None
            
@memoize('r2.models.mail_queue.has_opted_out')
def has_opted_out(email):
    o = Email.handler.opt_table
    s = sa.select([o.c.email], o.c.email == email, limit = 1)
    res = s.execute()
    return bool(res.fetchall())
    


        
        
    
