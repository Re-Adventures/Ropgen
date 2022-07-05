#!/usr/bin/python3
import subprocess
import capstone
import sys
import re

def print_info(s):
  print(f"\x1b[32m[*] {s}\x1b[0m")

def print_warning(s):
  print(f"\x1b[33m[+] {s}\x1b[0m")

def print_error(s):
  print(f"\x1b[31m[!] {s}\x1b[0m")

class ROP:
  # Can set the following parameters manually
  arch  = None
  start = None
  end   = None
  VA    = None

  supported_archs = {
    b"x86-64"  : "x64",
    b"80386"   : "x86",
    b"arm"     : "arm",
    b"aarch64" : "aarch64"
  }

  sections = None

  # Capstone disassembly engine, of the form 
  # Cs(CS_ARCH_X86, CS_MODE_64)
  dis_engine = None
  
  # Stores the gadgets in {address: instruction} form
  gadgets = {}
  interesting_gadget = {}

  # Patterns used for selecting gadgets which might be more useful than others
  patterns_x86     = [r"^(pop e..; )*ret"
                      r"^(pop e..; )call e..",
                      r"^(pop e..; )jmp e..",
                      r"^mov dword ptr \[e..\], e..; ret",
                     ]

  patterns_x64     = [r"^(pop e..; )*ret",
                      r"^(pop r..; )*ret",
                      r"^mov [dq]word ptr \[r..\], r..; ret",
                      r"^mov [dq]word ptr \[r..\], e..; ret",
                     ]

  patterns_arm     = [r"^pop {(...?, )+pc}",
                      r"^bl?x? ...?$",
                      r"^str.*? r..?, \[r..?\]",
                     ]
  patterns_aarch64 = None

  def __init__(self, binary_name):
    self.binary_name = binary_name
    print_info(f"Loaded file {binary_name}")
    self.set_arch()
    self.set_mode()
    self.set_offsets()

  def set_arch(self):
    '''This will determine the architecture & mode of the binary'''
    if self.arch is not None:
      print_info(f"Architecture: {self.arch}")
    else:
      print_warning("Determining the Binary Architecture")

      # Using the readelf utility to dump the elf header which will help us
      # determine the binary architecture
      try:
        proc = subprocess.Popen(["readelf", "-h", self.binary_name],
                                  stdout = subprocess.PIPE,
                                  stderr = subprocess.PIPE)
      except FileNotFoundError:
        print_error("readelf not found in system")
        print_info("Try manually setting the file information")
        exit(1)

      stdout, stderr = proc.communicate()

      if stderr:
        print_error("Some error while executing readelf")
        print_error(f"Error: {stderr}")
        exit(1)
      
      # Determining the binary architecture
      tmp = None
      header_info = stdout.splitlines()
      for entry in header_info:
        if b"machine:" in entry.lower():
          tmp = entry.split()[-1].lower()

      if tmp is None:
        print_error("Failed while determining the architecture of the binary")
        exit(1)

      self.arch = self.supported_archs[tmp]
      print_info(f"Architecture: {self.arch}")

  def set_mode(self):
    '''Setup the disassebmly engine based on the file architecture'''
    '''This will also choose the regex patterns for finding useful gadgets'''

    # Can use match (switch) statement here
    if "x64" == self.arch:
      self.dis_engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
      self.instruction_size = 1
      self.pattern = self.patterns_x64

    elif "x86" == self.arch:
      self.dis_engine = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
      self.instruction_size = 1
      self.pattern = self.patterns_x86

    elif "arm" == self.arch:
      # Can add thumb mode too
      self.dis_engine = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
      self.instruction_size = 4
      self.pattern = self.patterns_arm

    elif "aarch64" == self.arch:
      self.dis_engine = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
      self.instruction_size = 4 # XXX: Check this
      self.pattern = self.patterns_aarch64

    else:
      print_error(f"Unknown architecture {self.arch}")
      exit(1)


  def set_offsets(self):
    '''Finds out the start, end & VA address of the binary using readelf'''
    if self.start is not None and self.end is not None and self.VA is not None:
      print_info(f"Start:        {self.start}")
      print_info(f"End:          {self.end}")
      print_info(f"VA:           {self.va}")

    else:
      print_warning("Finding the start & end offsets")
      try:
        proc = subprocess.Popen(["readelf", "-SW", self.binary_name],
                                  stdout = subprocess.PIPE,
                                  stderr = subprocess.PIPE)
      except FileNotFoundError:
        print_error("readelf not found in system")
        print_info("Try manually setting the file information")
        exit(1)

      stdout, stderr = proc.communicate()

      if stderr:
        print_error("Some error while executing readelf")
        print_error(f"Error: {stderr}")
        exit(1)

      self.sections = stdout

      # TODO: Can all the sections which are executable
      # finding ".text" from the output
      ret = self.get_section_info(b".text")
      self.va, self.start = ret

      # The end of .text section would be the start of section which is after it
      self.end = self.find_next_section_info(b".text")


      # Aligning the sections to the page boundary
      self.va -= self.start

      # XXX: These might break the script
      # self.start &= 0xfffffffffffff000
      # self.end   |= 0xfff
      # self.end += 1

      
      print_info(f"Start:        {self.va + self.start:#08x}")
      print_info(f"End:          {self.va + self.end:#08x}")
      print_info(f"VA:           {self.va:#08x}")


  def get_section_info(self, section_name):
    '''Gets section information like start & end
      addresses from readelf's output'''
    
    tmp = None
    for line in self.sections.splitlines():
      if section_name in line:
        tmp = line
        break

    if tmp is None:
      print_error(f"Section {section_name} not found")
      return None, None

    # tmp is of the form
    #   [13] .text             PROGBITS        00000000004006e0 0006e0 0009d2 00
    tmp = tmp.split(section_name)[1]
    tmp = tmp.split()

    # Returns va & start of section respectively
    return (int(tmp[1], 16), int(tmp[2], 16))

  def find_next_section_info(self, section_name):
    '''Finds the start of section which is next to the section passed'''
    tmp = None
    sections_list = self.sections.splitlines()
    for i in range(len(sections_list)):
      if section_name in sections_list[i]:
        tmp = sections_list[i + 1]
        break

    if tmp is None:
      print_error(f"Next section of {section_name} not found")
      return None, None

    # tmp is of the form
    #   [14] .fini             PROGBITS        00000000004010b4 0010b4 000009
    # Extracting the section name
    tmp = tmp.split(b"]")[1]
    tmp = tmp.split()[0]

    return self.get_section_info(tmp)[1]


  def initialize(self):
    self.find_gadgets()
    self.check_interesting()

  def find_gadgets(self):
    '''Reads the file & returns gadgets'''
    with open(self.binary_name, 'rb') as fp:
      file_content = fp.read()

    for i in range(self.start,
                   self.end + self.instruction_size * 2,
                   self.instruction_size):

      disassembly = self.dis_engine.disasm(file_content[i:i+20], self.va + i)

      # finding the ret instruction
      tmp  = []
      found = False
      for decoded in disassembly:
        inst = f"{decoded.mnemonic} {decoded.op_str}"
        inst = inst.strip()
        tmp.append(inst)

        # Handle cases for arm & other architectures as well
        if self.check_end(inst):
          found = True
          break
      if not found:
        continue

      # Only unique gadgets are stored
      if tmp in self.gadgets.values():
        continue
      
      self.gadgets[self.va + i] = tmp
    
    to_write = ""
    for address, instructions in self.gadgets.items():
      to_write += f"{address:#08x}: " 
      to_write += "; ".join(instructions) + "\n"

    # Writing gadgets to file
    with open(f"{self.binary_name}_gadgets.asm", "w") as fp:
      fp.write(f"{len(self.gadgets)} gadgets found\n")
      fp.write(to_write)
      print_info(f"Gadgets written to {fp.name}")


  def check_end(self, instruction):
    '''This will determine if the gadget was found by checking if it ends'''
    '''with ret or similar instruction for given architecture'''
    if "x64" == self.arch:
      if "ret" == instruction or "syscall" == instruction:
        return True

      # Calling a register
      # XXX: r"call ([q|d]word )?\[?[re][abcds189].*?\]"
      if re.match(r"call r..?", instruction):
        return True


    elif "x86" == self.arch:
      # XXX: Check how int 80 is represented
      if "ret" == instruction or "int 0x80" == instruction:
        return True

      # Calling a register
      if re.match(r"call e..", instruction):
        return True

    elif "arm" == self.arch:
      # pop {r4, pc}
      if re.match(r"pop {.*?, pc}", instruction):
        return True

      # svc #0
      if re.match(r"s[vw][ci] .*?", instruction):
        return True

      # blx r3 or bx sb
      # XXX: Check this
      if re.match(r"bl?x? ..$", instruction):
        return True

    elif "aarch64" == self.arch:
      # svc #0
      if re.match(r"s[vw][ci] .*?", instruction):
        return True

      if "ret" == instruction:
        return True
      
      # XXX: Check below condition
      if re.match(r"pop {.*?, pc}", instruction):
        return True

    return False

  def check_interesting(self):
    '''This will try to select interesting gadgets from the found gadgets'''
    if self.pattern is None:
      return False

    to_write = ""
    for address, i in self.gadgets.items():
      gadget = '; '.join(i)
      for pattern in self.pattern:
        if re.match(pattern, gadget)\
           and i not in self.interesting_gadget.values():
          
          self.interesting_gadget[address] = i
          self.make_function(address, i)
          to_write += f"{address:#08x}: {gadget}\n"

    if len(self.interesting_gadget) > 0:
      with open(f"{self.binary_name}_gadgets.asm", "a") as fp:
        print(to_write)
        fp.write("\n\nInteresting Gadgets:\n" + to_write)
        print_info(f"Interesting Gadgets also written to {fp.name}")

  def make_function(self, address, gadget_list):
    pass

# Support for commandline processing
if len(sys.argv) <= 1:
  exit(0)

filename = sys.argv[1]
r = ROP(filename)
r.initialize()
