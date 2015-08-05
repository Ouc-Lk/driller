import logging

l = logging.getLogger("driller.tracer")

import cle
import angr
import simuvex

import os
import signal
import struct
import tempfile
import subprocess

class TracerEnvironmentError(Exception):
    pass

class TracerMisfollowError(Exception):
    pass

class TracerDynamicTraceOOBError(Exception):
    pass

class Tracer(object):
    '''
    Trace an angr path with a concrete input
    '''

    def __init__(self, binary, input, preconstrain=True):
        '''
        :param binary: path to the binary to be traced
        :param input: concrete input string to feed to binary
        :param preconstrain: should the path be preconstrained to the provided input
        '''

        self.binary       = binary
        self.input        = input
        self.preconstrain = preconstrain

        self.base = os.path.join(os.path.dirname(__file__), "..")

        self.driller_qemu = os.path.join(self.base, "driller-qemu", "driller-qemu-cgc")

        l.debug("self.driller_qemu: %s", self.driller_qemu)

        if not self._sane():
            l.error("one or more errors were detected in the environment")
            raise TracerEnvironmentError

        l.debug("accumulating basic block trace...")

        # does the input cause a crash?
        self.crash_mode = False

        # will set crash_mode correctly
        self.trace = self._dynamic_trace()

        l.debug("trace consists of %d basic blocks", len(self.trace))

        self.preconstraints = [ ]

        # initialize the basic block counter to 0
        self.bb_cnt = 0

        # keep track of the last basic block we hit
        self.previous = None

        self.path_group = self._prepare_paths()

### EXPOSED

    def next_branch(self):
        '''
        windup the tracer to the next branch

        :return: a path_group describing the possible paths at the next branch
                 branches which weren't taken by the dynamic trace are placed 
                 into the 'missed' stash and any preconstraints are removed from
                 'missed' branches.
        '''

        while len(self.path_group.active) == 1:
            current = self.path_group.active[0]

            #bb = self._current_bb()

            # if the dynamic trace stopped and we're in crash mode, we'll
            # return the current path

            # expected behavor, the dynamic trace and symbolic trace hit the 
            # same basic block
            if current.addr == self.trace[self.bb_cnt]:
                self.bb_cnt += 1 

            # angr steps through the same basic block twice when a syscall 
            # occurs
            elif current.addr == self.previous:
                pass 

            else:
                l.error("the dynamic trace and the symbolic trace disagreed")
                l.error("[%s] dynamic [0x%x], symbolic [0x%x]", self.binary,
                        self.trace[self.bb_cnt], current.addr)
                l.error("inputs was %r", self.input)
                raise TracerMisfollowError

            self.previous = current.addr
            self.path_group = self.path_group.step() 

            # if our input was preconstrained we have to keep on the lookout for unsat paths
            if self.preconstrain:
              self.path_group = self.path_group.stash(from_stash='unsat',
                                                      to_stash='active')

            self.path_group = self.path_group.drop(stash='unsat')

        l.debug("addrs: %r", map(lambda x: hex(x.addr), self.path_group.active))
        l.debug("taking the branch %x", self.trace[self.bb_cnt])
        self.path_group = self.path_group.stash_not_addr(
                                       self.trace[self.bb_cnt], 
                                       to_stash='missed')
        rpg = self.path_group

        self.path_group = self.path_group.drop(stash='missed')

        return rpg

    def run(self):
        '''
        run a trace to completion

        :return: a deadended path of a complete symbolic run of the program 
                 with self.input
        '''

        # keep calling next_branch until it quits
        branches = self.next_branch()
        while len(branches.active):
            try: 
                branches = self.next_branch()
            except IndexError:
                if self.crash_mode:
                    l.info("crash occured in basic block %x", self.trace[self.bb_cnt - 1])

                    # if a crash occured while trying to reach the next brach, we'll need to
                    # work of of self.path_group which should be a path which actually encountered
                    # the crashing basic block
                    branches = self.path_group.stash(from_stash='active', to_stash='crashed')
                    break

        # the caller is responsible for removing preconstraints

        return branches

    def remove_preconstraints(self, path):

        if not self.preconstrain:
            return

        new_constraints = [ ] 

        for c in path.state.se.constraints:
            for pc in self.preconstraints:
                c = c.replace(pc, path.state.se.true)
            new_constraints.append(c)

        path.state.se.constraints[:] = new_constraints
        path.state.downsize()

### SETUP

    def _sane(self):
        '''
        make sure the environment is sane and we have everything we need to do a trace
        '''
        sane = True        

        # check the binary
        if not os.access(self.binary, os.X_OK):
            if os.path.isfile(self.binary):
                l.error("\"%s\" binary is not executable", self.binary)
                sane = False
            else:
                l.error("\"%s\" binary does not exist", self.binary)
                sane = False

        if not os.access(self.driller_qemu, os.X_OK):
            if os.path.isfile(self.driller_qemu):
                l.error("driller-qemu-cgc is not executable")
                sane = False
            else:
                l.error("\"%s\" does not exist", self.driller_qemu)
                sane = False

        return sane

### DYNAMIC TRACING

    def _current_bb(self):
        try:
            self.trace[self.bb_cnt]
        except IndexError:
            if self.crash_mode:
                return None
            else:
                raise TracerDynamicTraceOOBError

    def _dynamic_trace(self):
        '''
        accumulate a basic block trace using qemu
        '''

        logfd, logfile = tempfile.mkstemp(prefix="driller-trace-", dir="/dev/shm")
        os.close(logfd)

        args = [self.driller_qemu, "-d", "exec", "-D", logfile, self.binary]

        with open('/dev/null', 'wb') as devnull:
            # we assume qemu with always exit and won't block
            p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=devnull, stderr=devnull)
            p.communicate(self.input)
            ret = p.wait()
            # did a crash occur?
            if ret < 0:
                if abs(ret) == signal.SIGSEGV or abs(ret) == signal.SIGILL:
                    l.info("input caused a crash (signal %d) during dynamic tracing", abs(ret))
                    l.info("entering crash mode")
                    self.crash_mode = True
            

        tfp = open(logfile, 'rb')
        trace = tfp.read()
        tfp.close()
        os.remove(logfile)

        addrs = [int(v.split('[')[1].split(']')[0], 16)
                 for v in trace.split('\n')
                 if v.startswith('Trace')]

        return addrs

    def _load_backed(self):
        '''
        load an angr project with an initial state seeded by qemu
        '''

        # get the backing by calling out to qemu
        backingfd, backingfile = tempfile.mkstemp(prefix="driller-backing-", dir="/dev/shm")

        args = [self.driller_qemu, "-predump", backingfile, self.binary]

        with open('/dev/null', 'wb') as devnull:
            # should never block, predump should exit at the first call which would block
            p = subprocess.Popen(args, stdout=devnull) 
            p.wait()

        # parse out the predump file
        memory = {}
        regs = {}
        with open(backingfile, "rb") as f:
            while len(regs) == 0:
                tag = f.read(4)
                if tag != "REGS":
                    start = struct.unpack("<I", tag)[0]
                    end = struct.unpack("<I", f.read(4))[0]
                    length = struct.unpack("<I", f.read(4))[0]
                    content = f.read(length)
                    memory[start] = content
                else:
                    # general purpose regs
                    regs['eax'] = struct.unpack("<I", f.read(4))[0]
                    regs['ebx'] = struct.unpack("<I", f.read(4))[0]
                    regs['ecx'] = struct.unpack("<I", f.read(4))[0]
                    regs['edx'] = struct.unpack("<I", f.read(4))[0]
                    regs['esi'] = struct.unpack("<I", f.read(4))[0]
                    regs['edi'] = struct.unpack("<I", f.read(4))[0]
                    regs['ebp'] = struct.unpack("<I", f.read(4))[0]
                    regs['esp'] = struct.unpack("<I", f.read(4))[0]

                    # d flag
                    regs['d']   = struct.unpack("<I", f.read(4))[0]

                    # eip
                    # adjust eip
                    regs['eip'] = struct.unpack("<I", f.read(4))[0] - 2

                    # fp regs
                    regs['st0'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st0'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st1'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st1'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st2'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st2'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st3'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st3'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st4'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st4'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st5'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st5'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st6'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st6'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['st7'] = struct.unpack("<Q", f.read(8))[0]
                    regs['st7'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    # fp tags
                    regs['fpu_t0'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t1'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t2'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t3'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t4'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t5'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t6'] = struct.unpack("B", f.read(1))[0]
                    regs['fpu_t7'] = struct.unpack("B", f.read(1))[0]

                    # ftop
                    regs['ftop'] = struct.unpack("<I", f.read(4))[0]

                    # sseround
                    regs['mxcsr'] = struct.unpack("<I", f.read(4))[0]

                    regs['xmm0'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm0'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm1'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm1'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm2'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm2'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm3'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm3'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm4'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm4'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm5'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm5'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm6'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm6'] |= struct.unpack("<Q", f.read(8))[0] << 64

                    regs['xmm7'] = struct.unpack("<Q", f.read(8))[0]
                    regs['xmm7'] |= struct.unpack("<Q", f.read(8))[0] << 64

        os.remove(backingfile)

        ld = cle.Loader(self.binary, main_opts={'backend': cle.loader.BackedCGC, 'memory_backer': memory, 'register_backer': regs, 'writes_backer': []})

        return angr.Project(ld)

### SYMBOLIC TRACING

    def _preconstrain_state(self, entry_state):
        '''
        preconstrain the entry state to the input
        '''

        stdin = entry_state.posix.get_file(0)

        for b in self.input:
            c = stdin.read(1) == entry_state.BVV(b)
            self.preconstraints.append(c)
            entry_state.se.state.add_constraints(c)

        stdin.seek(0)

    def _prepare_paths(self):

        project = self._load_backed()

        entry_state = project.factory.entry_state(add_options={simuvex.s_options.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY})

        # windup the basic block trace to the point where we'll begin symbolic trace
        while self.trace[self.bb_cnt] != project.entry + 2:
            self.bb_cnt += 1

        if self.preconstrain:
            self._preconstrain_state(entry_state)

        pg = project.factory.path_group(entry_state, immutable=True, save_unsat=True)
        return pg.step()

