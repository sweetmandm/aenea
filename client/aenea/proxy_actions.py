'''performs black magic on the dragonfly actions objects to force them to
   forward their actions to a remote server.'''

import aenea.communications
import aenea.config
import aenea.proxy_contexts

communication = aenea.communications.Proxy(
    aenea.config.HOST,
    aenea.config.PORT
    )

try:
    import dragonfly
except ImportError:
    import dragonfly_mock as dragonfly


class _Warn(dragonfly.ActionBase):
    def execute(self):
        pf = aenea.proxy_contexts._server_info().get('platform', None)
        print 'Warning: grammar can\'t handle server platform %s' % pf
        return False


class ProxyBase(object):
    pass


def _make_key_parser():
    from pyparsing import (Optional, Literal, Word, Group, Keyword,
                           StringStart, StringEnd, Or)
    digits = '0123456789'
    modifier_keywords = Word(''.join(aenea.config.MODIFIERS))
    key_symbols = Or([Keyword(symbol) for symbol in aenea.config.KEYS])
    pause_clause = Optional(Literal('/') + Word('.' + digits))
    modifier_clause = Optional(modifier_keywords + Literal('-'))
    key_hold_clause = Literal(':') + Or([Keyword(d) for d in ('up', 'down')])
    keypress_clause = Group(Group(pause_clause) + Group(Optional(Literal(':') +
                                                                 Word(digits))))

    return (StringStart() + Group(modifier_clause) + Group(key_symbols) +
            Group(key_hold_clause | keypress_clause) + Group(pause_clause) +
            StringEnd())


def _make_mouse_parser():
    from pyparsing import (Optional, Literal, Word, Group, Keyword,
                           Or, ZeroOrMore, Regex, Suppress)
    double = Regex(r'\d+(\.\d*)?([eE]\d+)?')
    coords = double + Suppress(Optional(Literal(','))) + double
    integer = Word('0123456789')
    move = (
        (Literal('(') + coords + Suppress(Literal(')'))) |
        (Literal('[') + coords + Suppress(Literal(']'))) |
        (Literal('<') + coords + Suppress(Literal('>')))
        )
    buttons = ('left', 'middle', 'right', 'wheelup', 'wheeldown')
    key = (Or([Keyword(sym) for sym in buttons]) | integer)

    press = (
        key +
        Optional(Literal(':') + (integer | (Literal('up') | Literal('down'))))
        + Optional(Literal('/') + integer)
        )

    list_element = Group(move | press)
    list_parser = list_element + ZeroOrMore(Suppress(',') + list_element)

    return list_parser


class ProxyKey(ProxyBase, dragonfly.DynStrActionBase):
    '''As Dragonfly's Key except the valid modifiers are a, c, s for alt,
       control and shift respectively, w indicates super and h
       indicates hyper.'''

    _parser = _make_key_parser()

    def _parse_spec(self, spec):
        proxy = aenea.communications.BatchProxy()
        for key in spec.split(','):
            modifier_part, key_part, command_part, outer_pause_part = \
                self._parser.parseString(key.strip())

            modifiers = ([aenea.config.MODIFIERS[c] for c in modifier_part[0]]
                         if modifier_part else [])
            key = key_part[0]

            # regular keypress event
            if len(command_part) == 1:
                ((pause_part, repeat_part),) = command_part

                repeat = int(repeat_part[1]) if repeat_part else 1
                pause = int(pause_part[1]) / 100. if pause_part else None
                if not repeat:
                    continue
                if pause is not None:
                    proxy.key_press(key=key, modifiers=modifiers, count=repeat,
                                    count_delay=pause)
                else:
                    proxy.key_press(key=key, modifiers=modifiers, count=repeat)
            # manual keypress event
            else:
                (_, direction) = command_part
                proxy.key_press(
                    key=key,
                    modifiers=modifiers,
                    direction=direction
                    )

            if outer_pause_part:
                proxy.pause(amount=int(outer_pause_part[1]) / 100.)

        return proxy._commands

    def _execute_events(self, commands):
        communication.execute_batch(commands)

###############################################################################
# Text


class ProxyText(ProxyBase, dragonfly.DynStrActionBase):
    def _parse_spec(self, spec):
        return spec

    def _execute_events(self, events):
        communication.server.write_text(text=events)

###############################################################################
# Mouse


class ProxyMouse(ProxyBase, dragonfly.DynStrActionBase):
    def _parse_spec(self, spec):
        proxy = aenea.communications.BatchProxy()
        list_parser = _make_mouse_parser()
        for item in list_parser.parseString(spec):
            if item[0] in '[<(':
                reference, x, y = item
                reference = {'[': 'absolute',
                             '<': 'relative',
                             '(': 'relative_active'}[reference]
                proxy.move_mouse(x=float(x), y=float(y),
                                 reference=reference,
                                 proportional=('.' in (x + y)))
            else:
                pause = None
                repeat = 1
                direction = 'click'
                key = item[0]
                if len(item) >= 3 and item[2] in ('down', 'up'):
                    assert len(item) in (3, 5)
                    direction = item[2]
                    if len(item) == 5:
                        pause = int(item[-1]) / 100.
                else:
                    if len(item) == 3:
                        assert item[1] in ':/'
                        if item[1] == ':':
                            repeat = int(item[2])
                        elif item[1] == '/':
                            pause = int(item[2]) / 100.
                    elif len(item) == 5:
                        assert item[1] == ':' and item[3] == '/'
                        repeat = int(item[2])
                        pause = int(item[4]) / 100.

                proxy.click_mouse(
                    button=key,
                    direction=direction,
                    count=repeat,
                    count_delay=pause
                    )

        return proxy._commands

    def _execute_events(self, commands):
        communication.execute_batch(commands)

###############################################################################
# click without moving mouse


class ProxyMousePhantomClick(ProxyMouse):
    '''specification is similar to that for mouse except you should only
       specify one move as more events may behave strangely.
       the intended usage is as these examples,
         '(55 274), 1'         # left click once at those coordinates
         '<9 222>, 1:2/10'     # left double-click at those coordinates
         '1:down, [1 1], 1:up' # drag what is there to the upper left corner
       '''

    def _parse_spec(self, spec):
        commands = ProxyMouse._parse_spec(self, spec)
        move, click = commands
        move[2]['phantom'] = click[2]['button']
        return [move]

###############################################################################
# do nothing


class NoAction(dragonfly.ActionBase):
    def execute(self):
        pass

###############################################################################
# take a different action depending on which context is currently active.


class ProxyContextAction(dragonfly.ActionBase):
    def __init__(self, default=None, actions=[]):
        self.actions = actions
        self.default = default

    def add_context(self, context, action):
        self.actions.append((context, action))

    def execute(self):
        for (context, action) in self.actions:
            win = dragonfly.Window.get_foreground()
            if context.matches(win.executable, win.title, win.handle):
                return action.execute()
        else:
            return self.default.execute()


class ProxyPlatformContext(dragonfly.Context):
    '''Class to choose between several contexts based on what the server says
       platform is. None key may be used for none of the above.'''

    def __init__(self, mapping):
        '''mapping is mapping from platform as string to Context.'''
        assert all(hasattr(x, 'matches') for x in mapping)
        self._mapping = mapping

    def matches(self, windows_executable, windows_title, windows_handle):
        platform = aenea.proxy_contexts._server_info().get('platform', None)
        chosen = self._mapping.get(platform, self._mapping.get(None, _Warn()))
        return chosen.matches(
            windows_executable,
            windows_title,
            windows_handle
            )


__all__ = [
    'ProxyKey',
    'ProxyText',
    'ProxyMouse',
    'NoAction',
    'ProxyMousePhantomClick',
    'ProxyContextAction'
    ]
