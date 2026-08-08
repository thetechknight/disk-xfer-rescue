#!/usr/bin/env python3
"""
emu8086.py - a tiny 8086 real-mode emulator for exercising the DOS sender
(TX.COM) off-hardware: it runs the real machine code, feeds it a scripted
worklist over a simulated 8250 UART, stubs INT 13h/16h/21h, and checks that
the sender images the requested sectors and exits cleanly.

WHAT IT IS GOOD FOR
    - Proving the *logic* of a build before you burn a floppy: handshake,
      worklist receive, range iteration, CRC framing, DATA/EOT flow.
    - Quick regression runs across fresh / single-range / multi-range worklists.

WHAT IT DOES **NOT** DO  (read this)
    - It is NOT cycle-accurate. It says nothing about timing, UART overrun, or
      how a 12 MHz 286 behaves under real interrupt load.
    - It does NOT validate opcode legality. It happily executes 286/386
      instructions, so it will run a binary that a real 8086/286 refuses.
      That blind spot is exactly what hid the original hang. Always run
      tools/check_8086.py as well - the two tools are complementary:
          check_8086.py  -> are all opcodes legal on an 8086?
          emu8086.py     -> given legal opcodes, is the logic correct?

USAGE
    python3 tools/emu8086.py tx-msdos/TX.COM          # self-test (fresh+resume)
    python3 tools/emu8086.py --steps 4000000 file.com
"""
import argparse
import sys


# Minimal 8086 real-mode emulator: enough to run TX.COM through its range loop.
# Stubs INT 13h/21h and the 8250 UART. Goal: find where/why it diverges.
import sys

class Emu:
    def __init__(self, com, seg=0x1000):
        self.m = bytearray(0x100000)
        self.seg = seg
        base = (seg<<4)+0x100
        self.m[base:base+len(com)] = com
        self.r = {k:0 for k in ['ax','bx','cx','dx','si','di','bp','sp']}
        self.sreg = {'cs':seg,'ds':seg,'es':seg,'ss':seg}
        self.ip = 0x100
        self.CF=self.ZF=self.SF=self.OF=self.PF=self.AF=0
        self.halt=False; self.exitcode=None
        # PSP: command tail at 0x80 (len byte + " 1 5\r")
        p=(seg<<4)
        tail=b" 1 5\r"
        self.m[p+0x80]=len(tail)
        self.m[p+0x81:p+0x81+len(tail)]=tail
        # UART incoming script (bytes the DOS side will 'receive')
        self.rx=bytearray()
        self.tx=bytearray()
        self.console=bytearray()
        self.steps=0
        self.hist=[]
    # ---- register helpers ----
    def g16(self,i): return self.r[['ax','cx','dx','bx','sp','bp','si','di'][i]]
    def s16(self,i,v): self.r[['ax','cx','dx','bx','sp','bp','si','di'][i]]=v&0xFFFF
    def g8(self,i):
        nm=['ax','cx','dx','bx'][i%4]; v=self.r[nm]
        return (v&0xFF) if i<4 else (v>>8)&0xFF
    def s8(self,i,val):
        nm=['ax','cx','dx','bx'][i%4]; v=self.r[nm]
        if i<4: self.r[nm]=(v&0xFF00)|(val&0xFF)
        else:   self.r[nm]=(v&0x00FF)|((val&0xFF)<<8)
    def lin(self,seg,off): return ((self.sreg[seg]<<4)+off)&0xFFFFF
    def rd(self,seg,off,n=1):
        a=self.lin(seg,off); return int.from_bytes(self.m[a:a+n],'little')
    def wr(self,seg,off,val,n=1):
        a=self.lin(seg,off); self.m[a:a+n]=(val&((1<<(8*n))-1)).to_bytes(n,'little')
    def push(self,v):
        self.r['sp']=(self.r['sp']-2)&0xFFFF; self.wr('ss',self.r['sp'],v,2)
    def pop(self):
        v=self.rd('ss',self.r['sp'],2); self.r['sp']=(self.r['sp']+2)&0xFFFF; return v
    def setzsp(self,v,n):
        m=(1<<(8*n))-1; v&=m
        self.ZF=1 if v==0 else 0
        self.SF=1 if v&(1<<(8*n-1)) else 0
        self.PF=1 if bin(v&0xFF).count('1')%2==0 else 0
    def fetch(self,n=1):
        v=self.rd('cs',self.ip,n); self.ip=(self.ip+n)&0xFFFF; return v
    def modrm(self):
        mb=self.fetch(); mod=mb>>6; reg=(mb>>3)&7; rm=mb&7
        if mod==3: return mod,reg,rm,None,None
        # 16-bit addressing
        if rm==0: base=(self.r['bx']+self.r['si'])
        elif rm==1: base=(self.r['bx']+self.r['di'])
        elif rm==2: base=(self.r['bp']+self.r['si'])
        elif rm==3: base=(self.r['bp']+self.r['di'])
        elif rm==4: base=self.r['si']
        elif rm==5: base=self.r['di']
        elif rm==6: base=self.r['bp'] if mod!=0 else 0
        elif rm==7: base=self.r['bx']
        seg='ds'
        if mod==0 and rm==6: base=self.fetch(2)
        elif mod==1: base+=self.sext(self.fetch(),1)
        elif mod==2: base+=self.fetch(2)
        if rm in (2,3) or (rm==6 and mod!=0): seg='ss'
        return mod,reg,rm,seg,base&0xFFFF
    def sext(self,v,n):
        b=8*n
        return v-(1<<b) if v&(1<<(b-1)) else v

    def run(self, worklist_bytes, maxsteps=2000000):
        # Build the UART rx script: the DOS side sends INFO, then we must feed CMD.
        # We feed CMD bytes as soon as the sender starts reading (after it sends INFO).
        self.cmd_bytes = worklist_bytes
        self.cmd_fed = False
        self.info_seen = 0
        try:
            while not self.halt and self.steps < maxsteps:
                self.steps+=1
                self.step()
        except Exception as ex:
            print("EXC:",ex)
            print("recent ip trail:", " ".join(hex(x) for x in self.hist[-30:]))
        return

    def uart_in(self, port):
        base=0x3F8
        if port==base+5:   # LSR: THRE|TEMT set; DR set if rx available
            dr = 1 if self.rx else 0
            return 0x60 | dr
        if port==base:     # RBR
            if self.rx: return self.rx.pop(0)
            return 0
        return 0
    def uart_out(self, port, val):
        base=0x3F8
        if port==base+3:   # LCR (holds DLAB)
            self.lcr=val&0xFF; return
        dlab=getattr(self,'lcr',0)&0x80
        if port==base+1: return          # IER or divisor-high
        if port!=base: return            # other UART regs: ignore
        if dlab: return                  # divisor-low write, not data
        if True:  # THR data write
            b=val&0xFF; self.tx.append(b)
            if not hasattr(self,'frame'): self.frame=bytearray()
            fr=self.frame
            # resync to SOH
            if not fr and b!=0x01: return
            fr.append(b)
            if len(fr)<2: return
            t=fr[1]
            need={0x49:16,0x44:521,0x45:12}.get(t)  # 'I','D','E'
            if need is None:
                self.frame=bytearray(); return
            if len(fr)>=need:
                if t==0x49:      # INFO -> send worklist
                    self.rx.extend(self.cmd_bytes)
                elif t==0x44:    # DATA -> ack
                    self.data_count=getattr(self,'data_count',0)+1
                    self.rx.append(0x06)
                elif t==0x45:    # EOT -> ack
                    self.eot_seen=True; self.rx.append(0x06)
                self.frame=bytearray()
    def do_int(self,n):
        ah=(self.r['ax']>>8)&0xFF; al=self.r['ax']&0xFF
        if n==0x21:
            if ah==0x09:
                off=self.r['dx']; s=b''
                while True:
                    c=self.rd('ds',off,1); 
                    if c==ord('$'): break
                    s+=bytes([c]); off=(off+1)&0xFFFF
                self.console.extend(s)
            elif ah==0x02:
                self.console.append(self.r['dx']&0xFF)
            elif ah==0x4C:
                self.halt=True; self.exitcode=al
            return
        if n==0x13:
            if ah==0x08:
                # geometry: 547cyl,4heads,38spt -> max cyl 546, max head 3
                c=546; h=3; s=38
                self.r['cx']=((c&0xFF)<<8)|(((c>>2)&0xC0)|s)
                self.r['dx']=(h<<8)|0x80
                self.CF=0
            elif ah==0x02 or ah==0x00:
                self.CF=0; self.r['ax']=(0<<8)|al  # success
            else:
                self.CF=0
            return
        if n==0x16:
            self.ZF=1  # no key
            return
        # unknown int
        raise Exception(f"INT {n:#x} ah={ah:#x} at ip={self.ip:#x}")

    def alu(self,op,a,b,n):
        m=(1<<(8*n))-1
        if op=='add': r=a+b
        elif op=='adc': r=a+b+self.CF
        elif op=='sub': r=a-b
        elif op=='sbb': r=a-b-self.CF
        elif op=='cmp': r=a-b
        elif op=='and': r=a&b
        elif op=='or': r=a|b
        elif op=='xor': r=a^b
        elif op=='test': r=a&b
        if op in ('add','adc','sub','sbb','cmp'):
            self.CF=1 if (r<0 or r>m) else 0
        else:
            self.CF=0; self.OF=0
        self.setzsp(r&m,n)
        return r&m

    def step(self):
        self.hist.append(self.ip)
        if len(self.hist)>60: self.hist.pop(0)
        seg_over=None; rep=None
        while True:
            op=self.fetch()
            if op==0x26: seg_over='es'; continue
            if op==0x2E: seg_over='cs'; continue
            if op==0x36: seg_over='ss'; continue
            if op==0x3E: seg_over='ds'; continue
            if op in (0xF3,0xF2): rep=op; continue
            break
        def eff_seg(s): return seg_over if seg_over else s
        # ---- opcodes ----
        if op==0xEA: pass
        if op==0x06: self.push(self.sreg['es']); return
        if op==0x07: self.sreg['es']=self.pop(); return
        if op==0x0E: self.push(self.sreg['cs']); return
        if op==0x16: self.push(self.sreg['ss']); return
        if op==0x17: self.sreg['ss']=self.pop(); return
        if op==0x1E: self.push(self.sreg['ds']); return
        if op==0x1F: self.sreg['ds']=self.pop(); return
        if 0x40<=op<=0x47:
            i=op-0x40; cf=self.CF; v=(self.g16(i)+1)&0xFFFF; self.setzsp(v,2); self.CF=cf; self.s16(i,v); return
        if 0x48<=op<=0x4F:
            i=op-0x48; cf=self.CF; v=(self.g16(i)-1)&0xFFFF; self.setzsp(v,2); self.CF=cf; self.s16(i,v); return
        if 0x50<=op<=0x57: self.push(self.g16(op-0x50)); return
        if 0x58<=op<=0x5F: self.s16(op-0x58,self.pop()); return
        if 0xB8<=op<=0xBF: self.s16(op-0xB8,self.fetch(2)); return
        if 0xB0<=op<=0xB7: self.s8(op-0xB0,self.fetch()); return
        if op==0xE8: rel=self.sext(self.fetch(2),2); self.push(self.ip); self.ip=(self.ip+rel)&0xFFFF; return
        if op==0xE9: rel=self.sext(self.fetch(2),2); self.ip=(self.ip+rel)&0xFFFF; return
        if op==0xEB: rel=self.sext(self.fetch(),1); self.ip=(self.ip+rel)&0xFFFF; return
        if op==0xC3: self.ip=self.pop(); return
        if op==0xCD: self.do_int(self.fetch()); return
        # jcc short
        jcc={0x72:('CF',1),0x73:('CF',0),0x74:('ZF',1),0x75:('ZF',0),
             0x76:None,0x77:None,0x78:('SF',1),0x79:('SF',0)}
        if op in (0x70,0x71,0x72,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7A,0x7B,0x7C,0x7D,0x7E,0x7F):
            rel=self.sext(self.fetch(),1); cond=self.jcccond(op); 
            if cond: self.ip=(self.ip+rel)&0xFFFF
            return
        if op==0x0F:
            op2=self.fetch()
            if 0x80<=op2<=0x8F:
                rel=self.sext(self.fetch(2),2); cond=self.jcccond(0x70|(op2-0x80))
                if cond: self.ip=(self.ip+rel)&0xFFFF
                return
            raise Exception(f"0F {op2:#x} at {self.ip:#x}")
        if op==0xE3: rel=self.sext(self.fetch(),1);  # jcxz
        if op==0xE3:
            if self.r['cx']==0: self.ip=(self.ip+rel)&0xFFFF
            return
        if op==0xE2: # loop
            rel=self.sext(self.fetch(),1); self.r['cx']=(self.r['cx']-1)&0xFFFF
            if self.r['cx']!=0: self.ip=(self.ip+rel)&0xFFFF
            return
        # mov r/m,r and r,r/m (88/89/8A/8B)
        if op in (0x88,0x89,0x8A,0x8B):
            w=op&1; d=(op>>1)&1; mod,reg,rm,s,off=self.modrm()
            n=2 if w else 1
            if d==0: # r/m <- reg
                val=self.g16(reg) if w else self.g8(reg)
                if mod==3: (self.s16 if w else self.s8)(rm,val)
                else: self.wr(eff_seg(s),off,val,n)
            else: # reg <- r/m
                if mod==3: val=self.g16(rm) if w else self.g8(rm)
                else: val=self.rd(eff_seg(s),off,n)
                (self.s16 if w else self.s8)(reg,val)
            return
        if op==0x8E: # mov sreg,r/m16
            mod,reg,rm,s,off=self.modrm()
            val=self.g16(rm) if mod==3 else self.rd(eff_seg(s),off,2)
            self.sreg[['es','cs','ss','ds'][reg]]=val; return
        if op==0x8C: # mov r/m16,sreg
            mod,reg,rm,s,off=self.modrm(); val=self.sreg[['es','cs','ss','ds'][reg]]
            if mod==3: self.s16(rm,val)
            else: self.wr(eff_seg(s),off,val,2)
            return
        # mov AL/AX,[moffs] and back (A0..A3)
        if op==0xA0: off=self.fetch(2); self.s8(0,self.rd(eff_seg('ds'),off,1)); return
        if op==0xA1: off=self.fetch(2); self.s16(0,self.rd(eff_seg('ds'),off,2)); return
        if op==0xA2: off=self.fetch(2); self.wr(eff_seg('ds'),off,self.r['ax']&0xFF,1); return
        if op==0xA3: off=self.fetch(2); self.wr(eff_seg('ds'),off,self.r['ax'],2); return
        # mov r/m, imm (C6/C7)
        if op in (0xC6,0xC7):
            w=op&1; mod,reg,rm,s,off=self.modrm(); n=2 if w else 1; imm=self.fetch(n)
            if mod==3: (self.s16 if w else self.s8)(rm,imm)
            else: self.wr(eff_seg(s),off,imm,n)
            return
        # ALU r/m,r and r,r/m for 00..3B (add,or,adc,sbb,and,sub,xor,cmp)
        aluops={0x00:'add',0x08:'or',0x10:'adc',0x18:'sbb',0x20:'and',0x28:'sub',0x30:'xor',0x38:'cmp'}
        base=op&0xF8
        if base in aluops and (op&7)<6:
            name=aluops[base]; w=op&1
            if (op&7)==4: # AL,imm8  (no modrm)
                r=self.alu(name,self.g8(0),self.fetch(),1)
                if name!='cmp': self.s8(0,r)
                return
            if (op&7)==5: # AX,imm16 (no modrm)
                r=self.alu(name,self.r['ax'],self.fetch(2),2)
                if name!='cmp': self.s16(0,r)
                return
            d=(op>>1)&1; mod,reg,rm,s,off=self.modrm(); n=2 if w else 1
            if (op&7) in (0,1): # r/m , reg
                a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
                b=self.g16(reg) if w else self.g8(reg)
                r=self.alu(name,a,b,n)
                if name!='cmp':
                    if mod==3:(self.s16 if w else self.s8)(rm,r)
                    else:self.wr(eff_seg(s),off,r,n)
            elif (op&7) in (2,3): # reg, r/m
                b=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
                a=self.g16(reg) if w else self.g8(reg)
                r=self.alu(name,a,b,n)
                if name!='cmp':(self.s16 if w else self.s8)(reg,r)
            return
        # test 84/85, A8/A9
        if op in (0x84,0x85):
            w=op&1; mod,reg,rm,s,off=self.modrm(); n=2 if w else 1
            a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
            b=self.g16(reg) if w else self.g8(reg); self.alu('test',a,b,n); return
        if op==0xA8: self.alu('test',self.g8(0),self.fetch(),1); return
        if op==0xA9: self.alu('test',self.r['ax'],self.fetch(2),2); return
        # grp1 80/81/83 : imm to r/m
        if op in (0x80,0x81,0x83):
            mod,reg,rm,s,off=self.modrm(); w=1 if op!=0x80 else 0; n=2 if w else 1
            if op==0x81: imm=self.fetch(2)
            elif op==0x83: imm=self.sext(self.fetch(),1)&0xFFFF
            else: imm=self.fetch()
            name=['add','or','adc','sbb','and','sub','xor','cmp'][reg]
            a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
            r=self.alu(name,a,imm,n)
            if name!='cmp':
                if mod==3:(self.s16 if w else self.s8)(rm,r)
                else:self.wr(eff_seg(s),off,r,n)
            return
        # inc/dec/push/call/jmp grp FE/FF
        if op in (0xFE,0xFF):
            mod,reg,rm,s,off=self.modrm(); w=1 if op==0xFF else 0; n=2 if w else 1
            a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
            if reg==0: r=self.alu('add',a,1,n); 
            if reg==0:
                if mod==3:(self.s16 if w else self.s8)(rm,r)
                else:self.wr(eff_seg(s),off,r,n); 
                return
            if reg==1: r=self.alu('sub',a,1,n); 
            if reg==1:
                if mod==3:(self.s16 if w else self.s8)(rm,r)
                else:self.wr(eff_seg(s),off,r,n)
                return
            raise Exception(f"FF /{reg} unimpl")
        # shifts C0/C1/D0/D1/D2/D3 grp2
        if op in (0xD0,0xD1,0xD2,0xD3,0xC0,0xC1):
            mod,reg,rm,s,off=self.modrm(); w=op&1; n=2 if w else 1
            if op in (0xD0,0xD1): cnt=1
            elif op in (0xD2,0xD3): cnt=self.r['cx']&0xFF
            else: cnt=self.fetch()
            a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
            m=(1<<(8*n))-1
            for _ in range(cnt):
                if reg==4: # shl
                    self.CF=1 if a&(1<<(8*n-1)) else 0; a=(a<<1)&m
                elif reg==5: # shr
                    self.CF=a&1; a=(a>>1)
                elif reg==0: # rol
                    self.CF=1 if a&(1<<(8*n-1)) else 0; a=((a<<1)|self.CF)&m
                else: raise Exception(f"shift /{reg}")
            if cnt: self.setzsp(a,n)
            if mod==3:(self.s16 if w else self.s8)(rm,a)
            else:self.wr(eff_seg(s),off,a,n)
            return
        # mul/div grp3 F6/F7
        if op in (0xF6,0xF7):
            mod,reg,rm,s,off=self.modrm(); w=op&1; n=2 if w else 1
            a=(self.g16(rm) if w else self.g8(rm)) if mod==3 else self.rd(eff_seg(s),off,n)
            if reg in (0,1): # test r/m,imm
                imm=self.fetch(n); self.alu('test',a,imm,n); return
            if reg==2: # not
                r=(~a)&((1<<(8*n))-1)
                if mod==3:(self.s16 if w else self.s8)(rm,r)
                else:self.wr(eff_seg(s),off,r,n)
                return
            if reg==3: # neg
                r=self.alu('sub',0,a,n)
                if mod==3:(self.s16 if w else self.s8)(rm,r)
                else:self.wr(eff_seg(s),off,r,n)
                return
            if reg==4: # mul
                if w: 
                    res=self.r['ax']*a; self.r['ax']=res&0xFFFF; self.r['dx']=(res>>16)&0xFFFF
                else:
                    res=(self.r['ax']&0xFF)*a; self.r['ax']=res&0xFFFF
                return
            if reg==6: # div
                if w:
                    dv=((self.r['dx']<<16)|self.r['ax'])
                    if a==0: raise Exception("div0")
                    q,r=divmod(dv,a)
                    if q>0xFFFF: raise Exception("div overflow")
                    self.r['ax']=q; self.r['dx']=r
                else:
                    dv=self.r['ax']
                    q,r=divmod(dv,a); self.r['ax']=((r&0xFF)<<8)|(q&0xFF)
                return
            if reg==5: # imul
                def se(x,bits): return x-(1<<bits) if x&(1<<(bits-1)) else x
                if w:
                    res=se(self.r['ax'],16)*se(a,16); self.r['ax']=res&0xFFFF; self.r['dx']=(res>>16)&0xFFFF
                else:
                    res=se(self.r['ax']&0xFF,8)*se(a,8); self.r['ax']=res&0xFFFF
                return
            if reg==7: # idiv
                def se(x,bits): return x-(1<<bits) if x&(1<<(bits-1)) else x
                if w:
                    dv=se((self.r['dx']<<16)|self.r['ax'],32); dr=se(a,16)
                    q=int(dv/dr); r=dv-q*dr; self.r['ax']=q&0xFFFF; self.r['dx']=r&0xFFFF
                else:
                    dv=se(self.r['ax'],16); dr=se(a,8); q=int(dv/dr); r=dv-q*dr
                    self.r['ax']=((r&0xFF)<<8)|(q&0xFF)
                return
            raise Exception(f"F7 /{reg}")
        # lodsb / stosb / rep
        if op==0xAC:
            self.s8(0,self.rd(eff_seg('ds'),self.r['si'],1)); self.r['si']=(self.r['si']+1)&0xFFFF; return
        if op==0xAA:
            cnt=self.r['cx'] if rep else 1
            for _ in range(cnt):
                self.wr('es',self.r['di'],self.r['ax']&0xFF,1); self.r['di']=(self.r['di']+1)&0xFFFF
            if rep: self.r['cx']=0
            return
        if op==0xEC: self.s8(0,self.uart_in(self.r['dx'])); return          # in al,dx
        if op==0xEE: self.uart_out(self.r['dx'],self.r['ax']&0xFF); return   # out dx,al
        if op==0xF8: self.CF=0; return
        if op==0xF9: self.CF=1; return
        if op==0xF5: self.CF^=1; return
        if op==0xFA or op==0xFB: return  # cli/sti
        if op==0xFC or op==0xFD: return  # cld/std
        if op==0x90: return
        raise Exception(f"unimpl opcode {op:#x} at ip={(self.ip-1)&0xFFFF:#x}")

    def jcccond(self,op):
        o=op&0x0F
        Z,C,S,O,P=self.ZF,self.CF,self.SF,self.OF,self.PF
        return [O,not O,C,not C,Z,not Z,C or Z,not(C or Z),
                S,not S,P,not P,S!=O,S==O,Z or (S!=O),not(Z or (S!=O))][o]

# ---- build a CMD (worklist) frame like rx.py send_command ----
def crc16(data):
    c=0xFFFF
    for b in data:
        c^=b<<8
        for _ in range(8):
            c=((c<<1)^0x1021)&0xFFFF if c&0x8000 else (c<<1)&0xFFFF
    return c
def cmd_frame(ranges):
    pl=len(ranges).to_bytes(2,'little')
    for a,b in ranges: pl+=a.to_bytes(4,'little')+b.to_bytes(4,'little')
    body=b'C'+pl
    return bytes([0x01])+body+crc16(body).to_bytes(2,'little')



# --------------------------------------------------------------------------
# CRC-16/CCITT-FALSE and CMD-frame builder (mirror of the host's send_command)
# --------------------------------------------------------------------------
def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def cmd_frame(ranges):
    pl = len(ranges).to_bytes(2, "little")
    for a, b in ranges:
        pl += a.to_bytes(4, "little") + b.to_bytes(4, "little")
    body = b"C" + pl
    return bytes([0x01]) + body + crc16(body).to_bytes(2, "little")


# --------------------------------------------------------------------------
# Test harness: tiny simulated disk + worklist host
# --------------------------------------------------------------------------
import types


def _make(com, geom=(2, 1, 4), bad_chs=None):
    """Build an emulator with a tiny CHS geometry and an optional bad sector.
    geom = (cyls, heads, spt); bad_chs = (ch, sector, dh) that returns a read error."""
    e = Emu(com)
    c, h, s = geom

    def do_int(self, n, _orig=Emu.do_int):
        ah = (self.r["ax"] >> 8) & 0xFF
        if n == 0x13 and ah == 0x08:
            self.r["cx"] = (((c - 1) & 0xFF) << 8) | ((((c - 1) >> 2) & 0xC0) | s)
            self.r["dx"] = (((h - 1) & 0xFF) << 8) | 0x80
            self.CF = 0
            return
        if n == 0x13 and ah == 0x02 and bad_chs is not None:
            ch = (self.r["cx"] >> 8) & 0xFF
            sec = self.r["cx"] & 0x3F
            dh = (self.r["dx"] >> 8) & 0xFF
            if (ch, sec, dh) == bad_chs:
                self.CF = 1
                self.r["ax"] = (0x04 << 8) | (self.r["ax"] & 0xFF)
                return
        return _orig(self, n)

    e.do_int = types.MethodType(do_int, e)
    return e


def _imaged_lbas(tx):
    i, out = 0, []
    while i < len(tx):
        if tx[i] == 0x01 and i + 1 < len(tx):
            t = tx[i + 1]
            if t == 0x44 and i + 6 <= len(tx):
                out.append(int.from_bytes(tx[i + 2:i + 6], "little")); i += 521; continue
            if t == 0x49:
                i += 16; continue
            if t == 0x45:
                i += 12; continue
        i += 1
    return out


def self_test(path, steps):
    com = open(path, "rb").read()
    total = 8  # 2*1*4
    cases = [
        ("fresh whole-disk",      [(0, total)],                 list(range(total))),
        ("resume single range",   [(4, 6)],                     [4, 5]),
        ("resume scattered",      [(1, 2), (4, 5), (6, 7)],     [1, 4, 6]),
    ]
    ok_all = True
    for label, wl, expect in cases:
        e = _make(com)
        e.run(cmd_frame(wl), maxsteps=steps)
        con = bytes(e.console).decode("latin1")
        got = _imaged_lbas(bytes(e.tx))
        ok = e.halt and "Done." in con and got == expect
        ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:20s} imaged={got} exit={e.exitcode}")
    return ok_all


def main():
    ap = argparse.ArgumentParser(description="8086 logic emulator / self-test for TX.COM")
    ap.add_argument("binary", help="path to the DOS .COM sender")
    ap.add_argument("--steps", type=int, default=3_000_000, help="instruction cap per run")
    args = ap.parse_args()
    print(f"emu8086 self-test: {args.binary}")
    ok = self_test(args.binary, args.steps)
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
