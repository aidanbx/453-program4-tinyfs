from asyncore import write
from curses.ascii import VT

from numpy import byte
from libDisk import *

###
# Phase 2: a simple file system
###

# MAX MEM SIZE: 65,536

# Error numbers
ERR_UNMOUNTED = -500
ERR_MOUNTED = -501
ERR_FS_FORMAT = -600
ERR_INCONSISTENT_BLOCKS = -400
ERR_DIR_ALIGNMENT = -601
ERR_DISK = -100
ERR_NO_FREE_BLOCK = -410


# Bytes in block
BLOCKSIZE = 256

# Bytes in metadata header
META_SIZE = 8

# 40 blocks worth of bytes available for every disk, can be updated
DEFAULT_DISK_SIZE = 10240
DISK_SIZE = DEFAULT_DISK_SIZE

# Default name for a new disk
DEFAULT_DISK_NAME = "tinyFSDisk"

# block types
FREE_BLOCKT = 2
INODE_BLOCKT = 3
DATA_BLOCKT = 4
DIRENT_NAMET = 5
DIRENT_NUMT = 6
FREE_MASKT = 7

# named indecies in block
TYPE_BYTE = 0
SIZE_BYTE = 1

# named superblock indecies
SUPER_BLOCK = 0
SB_NUM = 1
SB_NAME = 2
SB_FREE = 3

MAGIC_NUMBER = 0x5A


# ------------------------------------------------------------------------------------
# Dynamic resource table stores information about open files
# FD -> sought index, size, blocks
DRT = {}

# Super block internal structure (array of indecies, essentially)
SB = None

# Stores file name/descriptor pairs
DIRENT = {}

# Disk reference number from libdisk
DISK = None

# 0 is a free block, 1 is filled
FILLED = 1
EMPTY = 0
FREE_MASK = None


# Searches bit mask for first or last 0, sets it to 1
def claimFreeBlock(first=True):
    global FREE_MASK
    bNum = None
    if first:
        bNum = next((i for i, x in enumerate(FREE_MASK) if x == EMPTY), None)
    else:
        bNum = next((i for i, x in enumerate(
            reversed(FREE_MASK)) if x == EMPTY), None)
    FREE_MASK[bNum] = FILLED
    return bNum


# finds free block, writes data to it, returns bnum
# blocks: block numbers inode points to
def createInode(blocks, size, bType=INODE_BLOCKT):
    # set metadata
    idata = [0]*META_SIZE
    idata[TYPE_BYTE] = bType
    idata[SIZE_BYTE] = size
    idata += blocks
    # 8 bytes of meta data space then all the blocks

    ibuf = Buffer()
    ibuf.data_bytes = bytearray(idata)

    # find and claim free block, first if dirent or other, last if inode (arbitrary?)
    blockNum = claimFreeBlock(first=(bType != INODE_BLOCKT))
    if blockNum is None:
        print(f"createInode: ERR_NO_FREE_BLOCK")
        return ERR_NO_FREE_BLOCK

    if writeBlock(DISK, blockNum, ibuf) < 0:
        print(f"createInode: ERR_DISK")
        return ERR_DISK
    return blockNum


# updates metadata and writes block nums to inode
def updateInode(blockNum, newBlocks, newSize):
    idata = [0]*META_SIZE
    idata[SIZE_BYTE] = newSize
    idata += newBlocks

    ibuf = Buffer()
    ibuf.data_bytes = bytearray(idata)

    if writeBlock(DISK, blockNum, ibuf) < 0:
        print(f"updateInode: ERR_DISK")
        return ERR_DISK
    return blockNum


# from inode block number, reads all blocks pointed to by inode
def readViaInode(inodeBNum):
    iBuf = Buffer()
    if readBlock(DISK, inodeBNum, iBuf) < 0:
        print("readFromInode: ERR_DISK")
        return ERR_DISK

    iData = list(iBuf.data_bytes)
    meta = iData[:META_SIZE]
    blocks = iData[META_SIZE:]

    allData = []
    # read all blocks and combine them
    for bNum in blocks[:meta[SIZE_BYTE]]:
        buf = Buffer()
        readBlock(DISK, bNum, buf)
        bData = list(buf.data_bytes)
        # from block, read all data after meta data, up to size of block
        allData += bData[META_SIZE:bData[SIZE_BYTE]]
    return allData


# Write to a block and updates its metadata, returns data that can't fit into block
def writeBlockMeta(bNum, data):
    toWrite = [0]*META_SIZE
    # Either write all data or fill available space
    dataWriteLen = min(BLOCKSIZE-META_SIZE, len(data))
    toWrite[SIZE_BYTE] = dataWriteLen
    toWrite[TYPE_BYTE] = DATA_BLOCKT

    # only write data that we can
    toWrite += data[:dataWriteLen]
    wBuf = Buffer()
    wBuf.data_bytes = bytearray(toWrite)
    writeBlock(DISK, bNum, wBuf)

    return data[dataWriteLen:]  # returns data not written


# finda inode via block number, writes data (list), overwriting, updates metadata
def writeViaInode(inodeBNum, data):
    iBuf = Buffer()
    readBlock(DISK, inodeBNum, iBuf)
    iMeta = list(iBuf.data_bytes)[:META_SIZE]
    iData = list(iBuf.data_bytes)[META_SIZE:]

    restData = data
    numBlocksUsed = 0  # used to index into inode claimed blocks
    usedBNums = []  # keep track of what blocks we write to
    while len(restData) > 0:
        currBlock = None
        # if inode already claimed a block for this chunk
        if numBlocksUsed < iMeta[SIZE_BYTE]:
            currBlock = iData[numBlocksUsed]
        else:  # inode needs new block
            currBlock = claimFreeBlock(
                first=(iMeta[TYPE_BYTE] != INODE_BLOCKT))  # claim first block if dirent or freemask, last otherwise
        usedBNums.append(currBlock)
        # write data to block, updating its header, save data that couldn't fit
        restData = writeBlockMeta(currBlock, restData)
        numBlocksUsed += 1

    # update meta data for this inode
    newInodeData = iMeta
    newInodeData[SIZE_BYTE] = numBlocksUsed
    newInodeData += usedBNums

    newIBuf = Buffer()
    newIBuf.data_bytes = bytearray(newInodeData)
    writeBlock(DISK, inodeBNum, newIBuf)
    return 0


###
# Make an empty TinyFS of size nBytes on specified file
# Use libDisk to open file, format file to be mounted
# init data to 0x00, set magic numbers, init/write superblock and other metadata
#
# superblock entry in order: magic_number, root inode pointer, freeblock pointer
###
# tfs_mkfs(str, int) -> int (Success/Error Code)
def tfs_mkfs(filename, nBytes=DEFAULT_DISK_SIZE):
    status = 0
    numBlocks = int(nBytes/BLOCKSIZE)
    global DISK_SIZE
    DISK_SIZE = nBytes
    # open new disk, check errors
    diskNum = openDisk(filename, nBytes)
    if diskNum < 0:
        print("tfs_mkfs: ERR_DISK_CREATION")
        return diskNum  # ERROR

    global DISK
    DISK = diskNum
    SB = [0] * META_SIZE
    SB[0] = MAGIC_NUMBER

    global FREE_MASK
    FREE_MASK = [EMPTY] * numBlocks  # len(Freemask) = # blocks
    FREE_MASK[SUPER_BLOCK] = FILLED  # super block is not free

    freeMaskBlock = claimFreeBlock()
    # free block bit mask
    SB[SB_FREE] = createInode([freeMaskBlock], 1, FREE_MASKT)

    direntNumBlock = claimFreeBlock()
    # inode for inode block bumbers
    SB[SB_NUM] = createInode([direntNumBlock], 1, DIRENT_NUMT)

    direntNameBlock = claimFreeBlock()
    # file names stored via this INode
    SB[SB_NAME] = createInode([direntNameBlock], 1, DIRENT_NAMET)

    # save Free mask
    writeViaInode(SB[SB_FREE], FREE_MASK)

    supBuf = Buffer()
    supBuf.data_bytes = bytearray(SB)  # save SB to disk
    if writeBlock(DISK, SUPER_BLOCK, supBuf) < 0:
        return ERR_DISK

    return 0


###
# Mount a TinyFS file found on filename
# Verify type/format of FS, only one system mounted at a time
###
# tfs_mount(str) -> int (Success/Error Code)
def tfs_mount(filename):
    diskNum = openDisk(filename, 0)  # open without overwriting data
    global DISK
    DISK = diskNum
    tfs_unmount()

    superBlock = Buffer()
    if readBlock(diskNum, 0, superBlock) < 0:  # fetch super block
        return ERR_DISK  # Error

    global SB
    SB = list(superBlock.data_bytes)

    if SB[0] != 0x5A:
        return ERR_FS_FORMAT  # Invalid FS format

    # print(readViaInode(SB[SB_FREE]))

    for i in range(10):
        b = Buffer()
        readBlock(DISK, i, b)
        print(list(b.data_bytes))

    # fBuf = Buffer()
    # if readBlock(DISK, SB[SB_FREE], fBuf) < 0:
    #     print("tfS_mount: DISK ERROR")
    #     return ERR_DISK
    # global FREE_MASK
    # FREE_MASK =  readFromInode()# list(fBuf.data_bytes)

    # d1Buf = Buffer()
    # readBlock(DISK, SB[SB_NUM], d1Buf)

    # d2Buf = Buffer()
    # readBlock(DISK, SB[SB_NAME], d2Buf)

    # return 0
    # if len(direntBlocks) != len(direntNames):  # there should be an inode for every name
    #     return ERR_DIR_ALIGNMENT

    # # connect file names to inode block locations in local structure
    # global DIRENT
    # for i in range(len(direntBlocks)):
    #     DIRENT[direntNames[i]] = direntBlocks[i]


###
# Cleanly unmount the current filesystem
# write any important data to superblock, close all files (?)
###
# tfs_unmount() -> int (Success/Error Code)
def tfs_unmount():
    return 0
#     global DISK
#     if DISK is None:
#         return ERR_UNMOUNTED

#     supData = [0]*5
#     supData[SB_FREE] = FREE_HEAD  # free block head probably moved
#     supBuf = Buffer()
#     supBuf.data_bytes = bytearray(supData)
#     if writeBlock(DISK, SUPER_BLOCK, supBuf) < 0:
#         return ERR_DISK

#     if closeDisk(DISK) < 0:
#         return ERR_DISK

#     DISK = None
#     return -1


###
# Within the current mounted system, open a file to read and write
# create dynamic resource table entry with internal file pointer, etc.
###
# tfs_open(str) -> int (file descriptor/Error Code)
def tfs_open(name):
    inode = None
    global DIRENT
    if name in DIRENT:
        inode = DIRENT[name]
    else:
        pass
        #   create file?
        #


###
# Closes file at fileDescriptor, removes dynamic resource table entry
###
# tfs_close(fileDescriptor) -> int (Success/Error Code)
def tfs_close(fileDescriptor):
    pass


###
# Write to FD: buffer "buffer" of size "size", representing entire file's contents
# Set file pointer to start of file (0)
###
# tfs_write(fileDescriptor, Buffer, int) -> int (Success/Error Code)
def tfs_write(FD, buffer, size):
    pass


###
# Deletes file @ FD in FS and sets its blocks to free
###
# tfs_delete(fileDescriptor) -> int (Success/Error Code)
def tfs_delete(FD):
    pass


###
# read one bute from file @ FD and copies it to buffer
# increments file pointer by 1, return error if at end of file
###
# tfs_readByte(fileDescriptor, Buffer) -> int (Success/Error Code)
def tfs_readByte(FD, buffer):
    pass


###
# Change file pointer location of file @ FD to offset (relative to beginning)
###
# tfs_seek(fileDescriptor, int) -> int (Success/Error Code)
def tfs_seek(FD, offset):
    pass


###
#


# if __name__ == "__main__":
#     tfs_mkfs("disk1.dsk")
#     tfs_mount("disk1.dsk")
#     tfs_
